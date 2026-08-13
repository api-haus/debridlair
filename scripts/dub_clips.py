#!/usr/bin/env python3
"""Cut the moments a render struggled with, so they can be listened to.

Every render leaves a timing report saying which lines it had to squeeze and
which ones ran past their window anyway. That is enough to say an episode is
tight; it is not enough to say whether tight sounds bad. A line 0.9s over its
window might collide audibly with the next speaker, or might land in a pause
nobody notices, and no column in the report knows which.

So this takes the report and cuts the video at those exact lines, worst first,
with a few seconds either side. What comes out is short enough to watch back
to back, and cut from the finished episode rather than re-rendered, so it is
the real thing rather than an approximation of it.

Ask for `--clean` alongside to get lines the render had no trouble with at
all. A fault is only audible next to something that is not.

Usage:
    python3 scripts/dub_clips.py "Shirokuma Cafe" --worst 3 --tightest 2 --clean 1
    python3 scripts/dub_clips.py "Shirokuma Cafe" 4 --worst 5
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_season import ROOT, episodes, read_plan, resolve  # noqa: E402

OUTBOX = ROOT / "dub" / "clips"

# Room either side of the line. More after than before, because what a line
# running long actually does is land on the next speaker, and the collision is
# the thing being listened for.
PAD_BEFORE, PAD_AFTER = 3.0, 4.0

# Small and quick: these are for judging a performance, not for keeping.
HEIGHT, CRF = 720, 23


def readable(text, limit=40):
    """A line of dialogue, cut down to something that fits in a filename."""
    flat = re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", "", text)).strip()
    return (flat[:limit].rstrip() or "line")


def spoken_text(plan, entry):
    """What each line said, by id, so a clip can be named after its words."""
    if not entry["utterances"].exists():
        return {}
    return {u["id"]: u["text"] for u in json.loads(entry["utterances"].read_text())}


def pick(rows, kind, count):
    """The lines worth hearing, worst first.

    A line with no window at all is dropped from the overrun list: a title
    card counts its whole length as overflow because there was never room for
    it, which is a fact about the card rather than about the read.
    """
    if kind == "worst":
        wanted = [r for r in rows if r["overflow"] > 0.25 and r["available"] > 0]
        return sorted(wanted, key=lambda r: -r["overflow"])[:count]
    if kind == "tightest":
        wanted = [r for r in rows if r["compression"] > 1.01]
        return sorted(wanted, key=lambda r: -r["compression"])[:count]
    wanted = [r for r in rows if r["compression"] <= 1.01 and r["overflow"] <= 0.05
              and r["available"] > 1.5 and r["held"] > 1.5]
    return sorted(wanted, key=lambda r: -r["held"])[:count]


def cut(source, row, label, destination):
    start = max(0.0, row["start_seconds"] - PAD_BEFORE)
    span = (row["start_seconds"] + row["held"] + PAD_AFTER) - start

    # Seeking before the input is accurate here because the video is being
    # re-encoded: nothing is snapped to a keyframe, which a stream copy would
    # do and which would put the clip seconds away from the line it is about.
    subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}",
                    "-i", str(source), "-t", f"{span:.3f}",
                    "-map", "0:v:0", "-map", "0:a:0",
                    "-vf", f"scale=-2:{HEIGHT}", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    str(destination), "-y"], check=True)
    return start, span


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show", help="show title or any of its aliases")
    parser.add_argument("episode", nargs="?", type=int,
                        help="one episode; omitted means every finished one")
    parser.add_argument("--worst", type=int, default=0, metavar="N",
                        help="N lines that ran furthest past their window")
    parser.add_argument("--tightest", type=int, default=0, metavar="N",
                        help="N lines squeezed hardest to fit")
    parser.add_argument("--clean", type=int, default=0, metavar="N",
                        help="N lines the render had no trouble with, to hear against")
    args = parser.parse_args()

    if not (args.worst or args.tightest or args.clean):
        raise SystemExit("ask for --worst, --tightest or --clean")

    paths = resolve(args.show)
    if len(paths) > 1:
        raise SystemExit("name which show: "
                         + ", ".join(read_plan(path)["show"] for path in paths))
    plan = read_plan(paths[0])

    # Pooled across the season, then cut, so "the three worst" means the three
    # worst there are rather than the three worst in whichever episode is first.
    pool, unplaced = [], []
    for entry in episodes(plan):
        if args.episode is not None and entry["number"] != args.episode:
            continue
        report = Path(str(entry["output"]) + ".timing.json")
        if not report.exists():
            continue
        rows = json.loads(report.read_text())

        # Reports written before the render recorded where it put each line
        # cannot be cut at all. Named rather than skipped quietly: an episode
        # missing from a worst-lines list reads as an episode with nothing
        # wrong in it, which is the opposite of what this means.
        if not all("start_seconds" in row and "held" in row for row in rows):
            unplaced.append(entry["number"])
            continue

        said = spoken_text(plan, entry)
        for row in rows:
            row["episode"], row["said"] = entry, said.get(row["id"], "")
        pool.append(rows)

    if unplaced:
        print(f"not cuttable, rendered before the timing report recorded "
              f"placement: {', '.join(f'E{n:02d}' for n in unplaced)}\n"
              f"re-render one to include it here\n")
    if not pool:
        raise SystemExit("no finished episodes carry placement to cut from")

    chosen = []
    for kind, count in (("worst", args.worst), ("tightest", args.tightest),
                        ("clean", args.clean)):
        if count:
            everything = [row for rows in pool for row in rows]
            chosen += [(kind, row) for row in pick(everything, kind, count)]

    OUTBOX.mkdir(parents=True, exist_ok=True)
    for kind, row in chosen:
        entry = row["episode"]
        number, season = entry["number"], plan["season"]
        measure = (f"over {row['overflow']:.2f}s" if kind == "worst" else
                   f"squeezed {row['compression']:.2f}x" if kind == "tightest"
                   else "clean")
        name = (f"S{season:02d}E{number:02d} {kind} - {row['speaker']} "
                f"{measure} - {readable(row['said'])}.mp4")
        start, span = cut(entry["output"], row, kind, OUTBOX / name)
        print(f"S{season:02d}E{number:02d} {int(start) // 60}:{int(start) % 60:02d} "
              f"{span:>4.0f}s  {row['speaker']:<12} {measure:<18} {row['said'][:44]}")

    print(f"\n{len(chosen)} clips in {OUTBOX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
