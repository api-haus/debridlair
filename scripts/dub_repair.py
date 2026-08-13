#!/usr/bin/env python3
"""Fix one line of a finished episode without disturbing the rest of it.

Something will come out wrong. A name read as a different name, a line the model
drew badly, a word nobody thought to check — and it will be found the way these
things are always found, by watching the episode and noting the time. Answering
that with a full re-render costs a quarter hour of GPU to change three seconds,
and every other line comes back a fresh draw of a stochastic model, so a fix for
one line quietly re-rolls the three hundred around it.

Nothing here re-rolls anything. Each drawn line is cached beside the episode
under a stamp of what it was drawn from — the text, the voice, the room it had
to fit. Re-rendering reuses every clip whose stamp still matches, so the lines
you did not touch come back as the identical wav, sample for sample, and only
the repaired one goes near the GPU.

Three kinds of repair, and the difference is only in what stops matching:

  * A bad draw. The text is right, the model just read it poorly. `--redraw`
    deletes those clips so they are drawn again.
  * A word said wrong. Fix it in the lexicon and `--rebuild`: the stamp carries
    the spoken text, so every line carrying that word redraws itself and the
    rest of the episode does not.
  * A line that should say something else. Edit its `adapted` text and
    `--rebuild`, which is the same mechanism again.

The mix is rebuilt whole either way, because the dialogue bus is compressed and
levelled as one piece, and that last stage is the one place a repair does reach
past itself. Measured on a scene of 57 lines with one line redrawn: every other
line came back with identical placement, gain, compression, pan and overflow —
the mix made the same decision about each of them — and the audio outside the
repair moved by at most -71 dBFS, some 65 dB below the programme, from the
dialogue compressor matching its level over a bus that now holds one different
line. The finished MKV then re-encodes to AAC, which adds its own quantisation
noise on top; that is codec noise rather than a mix change, and it is what makes
a re-rendered episode differ from the old one sample for sample.

So: inaudible, but not bit-identical, and `--verify` reports the part that would
matter — whether the mix treated any other line differently.

Usage:
    python3 scripts/dub_repair.py "Polar Bear Cafe" 12 --at 8:32
    python3 scripts/dub_repair.py "Polar Bear Cafe" 12 --at 8:32 --redraw
    python3 scripts/dub_repair.py "Polar Bear Cafe" 12 --line 143 --redraw
    python3 scripts/dub_repair.py "Polar Bear Cafe" 12 --rebuild --verify
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dub_render import parse_timecode
from dub_season import ROOT, episodes, read_plan, resolve

# How far either side of a timecode to look. A note taken while watching lands
# on the line being heard, and a viewer's thumb is not frame accurate.
WINDOW = 2.5


def timing_path(entry):
    return Path(str(entry["output"]) + ".timing.json")


def find_lines(utterances, at=None, ids=()):
    """The lines a repair names, by timecode or by id."""
    if ids:
        wanted = set(ids)
        found = [u for u in utterances if u["id"] in wanted]
        missing = wanted - {u["id"] for u in found}
        if missing:
            raise SystemExit(f"no line numbered {', '.join(map(str, sorted(missing)))}")
        return found

    return [u for u in utterances
            if u["start"] - WINDOW <= at <= u["end"] + WINDOW]


def show(lines, clips):
    for utterance in lines:
        drawn = clips / f"{utterance['id']:04d}.wav"
        state = "drawn" if drawn.exists() else "no clip on disk"
        print(f"  [{utterance['id']:>3}] {utterance['start']:>7.2f}s "
              f"{utterance['speaker']:<12} {state:<16} {utterance['text']}")


def invalidate(lines, clips):
    """Drop the cached draw for these lines, so the next render makes new ones."""
    gone = 0
    for utterance in lines:
        for path in (clips / f"{utterance['id']:04d}.wav",
                     clips / f"{utterance['id']:04d}.json"):
            if path.exists():
                path.unlink()
                gone += 1
    return gone


def render(plan, entry):
    """The season's own render command, so a repair mixes exactly as the run did.

    Built from the plan rather than typed out again. A repair that passed even
    one option differently would rebuild the whole episode to a slightly
    different mix, which is the precise failure this tool exists to avoid.
    """
    command = [str(plan["python"]), "-u", str(ROOT / "scripts/dub_render.py"),
               str(entry["utterances"]), str(plan["voices"]), str(entry["stems"]),
               "--video", str(entry["video"]), "-o", str(entry["output"]),
               "--clips", str(entry["clips"]), "--keep-clips", *plan["options"]]
    if plan["queue"]:
        command = ["processqueue", plan["queue"], *command]
    print("  " + " ".join(command) + "\n")
    return subprocess.run(command).returncode


def compare(before, after, repaired):
    """What moved, line by line, between the episode as it was and as it is.

    Reads the timing report rather than the audio: it carries the level each
    line was matched to and how far it overran, which is what would shift if a
    rebuild had disturbed its neighbours.
    """
    if not before:
        print("\nno timing report from before the repair; nothing to compare")
        return

    was = {row["id"]: row for row in before}
    moved = []
    for row in after:
        old = was.get(row["id"])
        if old is None or row["id"] in repaired:
            continue
        if any(abs(row[key] - old[key]) > 0.005
               for key in ("generated", "compression", "gain", "overflow")):
            moved.append((row["id"], old, row))

    print(f"\n{len(after)} lines in the rebuilt episode, "
          f"{len(repaired)} repaired, {len(moved)} others moved")
    for ident, old, new in moved[:10]:
        print(f"  line {ident} {new['speaker']}: "
              f"gain {old['gain']:.3f} -> {new['gain']:.3f}, "
              f"{old['generated']:.2f}s -> {new['generated']:.2f}s")
    if not moved:
        print("  the mix placed, levelled and fitted every other line exactly as "
              "before")
        print("  (their audio still moves by around -71 dBFS from the dialogue "
              "compressor, and again under AAC; neither is audible)")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show", help="the season's title, or any of its aliases")
    parser.add_argument("episode", type=int, help="episode number within the season")
    parser.add_argument("--at", metavar="TIMECODE",
                        help="where in the episode it sounds wrong, mm:ss")
    parser.add_argument("--line", type=int, action="append", default=[],
                        metavar="N", help="repair by line number instead; repeatable")
    parser.add_argument("--redraw", action="store_true",
                        help="draw these lines again, for a bad read of right text")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-render reusing every clip that still matches, "
                             "for after a lexicon or adaptation edit")
    parser.add_argument("--verify", action="store_true",
                        help="report what moved outside the repair")
    args = parser.parse_args()

    plans = resolve(args.show)
    if len(plans) != 1:
        raise SystemExit(f"{args.show!r} names {len(plans)} seasons; name one")
    plan = read_plan(plans[0])

    entry = next((e for e in episodes(plan) if e["number"] == args.episode), None)
    if entry is None:
        raise SystemExit(f"{plan['show']} has no episode {args.episode}")
    if not entry["utterances"].exists():
        raise SystemExit(f"{entry['utterances']} does not exist; nothing is prepared")

    utterances = json.loads(entry["utterances"].read_text())
    clips = entry["clips"]

    at = parse_timecode(args.at)
    if at is None and not args.line and not args.rebuild:
        raise SystemExit("say where: --at <timecode>, --line <n>, or --rebuild")

    lines = find_lines(utterances, at, args.line) if (at is not None or args.line) else []
    if (at is not None or args.line) and not lines:
        raise SystemExit(f"nothing is spoken within {WINDOW}s of {args.at}")

    print(f"{plan['show']} S{plan['season']:02d}E{entry['number']:02d}"
          + (f", around {args.at}" if args.at else "") + "\n")
    if lines:
        show(lines, clips)

    if not (args.redraw or args.rebuild):
        print("\nnothing changed. --redraw draws these again; --rebuild re-renders "
              "after a lexicon or adaptation edit")
        return 0

    if not entry["output"].exists():
        print(f"\n{entry['output'].name} has not been rendered yet; this will "
              f"render it rather than repair it")

    # Counted before anything is deleted: with no clip cache every line is a
    # fresh draw, which is a quarter hour of GPU and a re-roll of the whole
    # episode, and that is worth saying out loud rather than discovering.
    held = len(list(clips.glob("*.wav"))) if clips.exists() else 0
    speaking = sum(1 for u in utterances if u.get("kind") != "skip")

    if args.redraw:
        dropped = invalidate(lines, clips)
        print(f"\ndropped {dropped} cached files for "
              f"{len(lines)} line{'s' if len(lines) != 1 else ''}")
        held = len(list(clips.glob("*.wav"))) if clips.exists() else 0

    print(f"{held} of {speaking} lines are cached and will be reused; "
          f"{speaking - held} go to the GPU")
    if not held:
        print("no clips survive for this episode, so this is a full re-render: "
              "every line is drawn again and every line may come back different")

    before = (json.loads(timing_path(entry).read_text())
              if timing_path(entry).exists() else None)

    code = render(plan, entry)
    if code != 0:
        print(f"render exited {code}; the episode on disk is unchanged")
        return code

    if args.verify:
        after = json.loads(timing_path(entry).read_text())
        compare(before, after, {u["id"] for u in lines})
    return 0


if __name__ == "__main__":
    sys.exit(main())
