#!/usr/bin/env python3
"""Render a prepared season one episode at a time, and stop when asked.

A season is nine or ten hours of GPU. Nobody sits through that in one go, so
the run has to be something you can walk away from, put down, and pick up from
another session days later without wondering what state it left behind.

Three things make that true, and none of them is a progress file somebody has
to remember to update:

  * The plan is on disk. Everything the run needs — which show, which bank,
    where the stems are — lives in one JSON file, so resuming is the same
    command as starting.
  * Where it got to is read off the filesystem, never recorded. An episode is
    done because the episode is there. A line is drawn because the clip is
    there. Nothing can claim progress that did not happen.
  * Stopping is a file. `PAUSE` in the work directory stops the run between
    lines, from any session, whether or not it owns the process.

A season is named by its title rather than by where its files happen to live,
and any of the titles it goes by will do. Naming nothing means all of them,
which is what you want from a status and almost never from a run.

Usage:
    python3 scripts/dub_season.py --status                      # every season
    python3 scripts/dub_season.py "Shirokuma Cafe" --status     # one of them
    python3 scripts/dub_season.py "Shirokuma Cafe"              # run, or carry on
    python3 scripts/dub_season.py "Shirokuma Cafe" --limit 1    # just the next one
    python3 scripts/dub_season.py --halt                        # stop everything

Plans are found at `dub/*/season.json`. One, with the defaults filled in:

    {
      "show":    "Polar Bear Cafe",
      "aliases": ["Shirokuma Cafe"],
      "season":  1,
      "work":    "dub/work",
      "voices":  "dub/voices_test",
      "stems":   "dub/stems/htdemucs",
      "source":  "dub/source",
      "library": "dub/finished/tv",
      "python":  "dub/.venv/bin/python",
      "queue":   "gpu",
      "options": []
    }

`options` is passed to dub_render.py verbatim, which is where a season-wide
`--solo` or `--mix voiceover` belongs. Paths are relative to the repository,
not to wherever the run was started.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# The one brake. Any session can set it, including one that has no idea which
# process is running or where; the render checks it between lines and this
# driver checks it between episodes.
PAUSE = "PAUSE"

# What dub_render.py returns when it stopped because it was asked to, as
# opposed to because something went wrong.
EXIT_PAUSED = 75

DEFAULTS = {"work": "dub/work", "voices": "dub/voices_test",
            "stems": "dub/stems/htdemucs", "source": "dub/source",
            "library": "dub/finished/tv", "python": "dub/.venv/bin/python",
            "queue": "gpu", "season": 1, "options": [], "aliases": []}

# Every path in a plan is written the way the docs write it, relative to the
# repository, and resolved against the repository rather than against whatever
# directory the run happens to start in. A season that renders from one shell
# and not from another is the kind of fault nobody finds until the day they
# resume it from somewhere else.
ROOT = Path(__file__).resolve().parent.parent


def read_plan(path):
    plan = {**DEFAULTS, **json.loads(Path(path).read_text())}
    if not plan.get("show"):
        raise SystemExit(f"{path} names no show")
    for key in ("work", "voices", "stems", "source", "library", "python"):
        plan[key] = ROOT / plan[key]
    return plan


def find_plans():
    """Every season plan there is, wherever its show keeps its working files."""
    return sorted((ROOT / "dub").glob("*/season.json"))


def titles(plan):
    return [plan["show"], *plan.get("aliases", [])]


def normalise(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve(wanted):
    """Which plans a request names — a path, a title, or nothing for all of them.

    Shows travel under more than one title: a season prepared as Polar Bear
    Cafe gets asked for as Shirokuma Cafe. So a plan may list `aliases`, and
    any of them names it. Exact matches first, and only if none of those hit is
    a partial one accepted, so `Attack on Titan` cannot be answered by
    `Attack on Titan Junior High` while both exist.
    """
    paths = find_plans()
    if wanted is None:
        return paths
    if Path(wanted).is_file():
        return [Path(wanted)]

    asked = normalise(wanted)
    known = {path: titles(read_plan(path)) for path in paths}
    hits = [path for path, names in known.items()
            if any(normalise(name) == asked for name in names)]
    hits = hits or [path for path, names in known.items()
                    if any(asked in normalise(name) or normalise(name) in asked
                           for name in names)]
    if not hits:
        have = ", ".join(sorted(name for names in known.values() for name in names))
        raise SystemExit(f"no season plan for {wanted!r}. Prepared: {have or 'none'}")
    return hits


def episodes(plan):
    """Every episode in the season, prepared or not.

    Parsed scripts are discovered rather than listed, so an episode prepared
    later joins the run by existing and nobody extends a range by hand. But
    discovery alone cannot see an episode that was never prepared, and a
    season one episode in then reports as fully rendered — which is the one
    thing a progress tool must never say. `episodes` in the plan is how many
    the season actually has, and the ones with no parse are counted and shown
    as missing rather than quietly left out of the total.
    """
    season = plan["season"]
    numbers = {int(re.search(r"e(\d+)$", parsed.name.split(".")[0]).group(1))
               for parsed in plan["work"].glob(f"s{season:02d}e*.utterances.json")}
    numbers |= set(range(1, (plan.get("episodes") or 0) + 1))

    found = []
    for number in sorted(numbers):
        stem = f"s{season:02d}e{number:02d}"
        parsed = plan["work"] / f"{stem}.utterances.json"
        name = (f"S{season:02d}E{number:02d} - {plan['show']} - "
                f"{number:02d} [Dub].mkv")
        found.append({
            "number": number,
            "stem": stem,
            "utterances": parsed,
            "stems": plan["stems"] / f"{stem}.audio",
            "video": plan["source"] / f"{stem}.mkv",
            "clips": plan["work"] / "clips" / stem,
            "output": plan["library"] / plan["show"] / f"Season {season:02d}" / name,
        })
    return found


def progress(entry):
    """Where one episode stands, measured off the disk and nothing else."""
    if entry["output"].exists():
        return "done", 0, 0
    lines = (sum(1 for u in json.loads(entry["utterances"].read_text())
                 if u.get("kind") != "skip")
             if entry["utterances"].exists() else 0)
    drawn = len(list(entry["clips"].glob("*.wav"))) if entry["clips"].exists() else 0
    return ("part" if drawn else "waiting"), drawn, lines


def missing(entry):
    """What an episode needs before it can be rendered."""
    wanted = [entry["utterances"], entry["stems"] / "no_vocals.wav",
              entry["stems"] / "vocals.wav", entry["video"]]
    return [path for path in wanted if not path.exists()]


def report(plan, found, path=None):
    done = [e for e in found if e["output"].exists()]
    print(f"{plan['show']}, season {plan['season']:02d} — "
          f"{len(done)} of {len(found)} episodes rendered"
          + (f"  [{path}]" if path else "") + "\n")

    for entry in found:
        state, drawn, lines = progress(entry)
        if state == "done":
            size = entry["output"].stat().st_size / 1e6
            note = f"{size:>6.0f} MB"
        elif state == "part":
            note = f"{drawn} of {lines} lines drawn, will resume there"
        else:
            gaps = missing(entry)
            note = (f"{lines} lines" if not gaps else
                    "NOT PREPARED: missing " + ", ".join(p.name for p in gaps))
        print(f"  {state:<8} S{plan['season']:02d}E{entry['number']:02d}  {note}")

    brake = plan["work"] / PAUSE
    if brake.exists():
        print(f"\npaused: {brake} is set — {brake.read_text().strip()}")
        print("running this tool again clears it")


def halt(plan, why):
    brake = plan["work"] / PAUSE
    brake.write_text(f"{why} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    return brake


def refresh_emby():
    """Ask Emby to pick up the episode that just landed.

    Not fatal if it fails. The library scan runs on its own schedule as well,
    so the worst a failure here costs is the episode showing up later.
    """
    try:
        key = (ROOT / "sync-state/emby_api_key").read_text().strip()
        url = f"http://localhost:8096/emby/Library/Refresh?api_key={key}"
        request = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=30):
            print("  Emby refreshed")
    except Exception as error:                       # noqa: BLE001 - advisory only
        print(f"  could not refresh Emby ({error}); it will scan on its own")


def render(plan, entry):
    """Run one episode, and pass a stop on to it rather than killing it.

    A signal to this driver is turned into the pause file rather than forwarded
    down the process tree. The render is behind processqueue, so what a
    forwarded signal would reach is the queue wrapper, not the python holding
    the model; the file reaches the render itself whatever is in between.
    """
    # Unbuffered, because the log of a run this long is the only account of it
    # anyone has while it is going. Block-buffered, a reader sees nothing for
    # minutes at a stretch and cannot tell a slow episode from a stuck one.
    command = [str(plan["python"]), "-u", str(ROOT / "scripts/dub_render.py"),
               str(entry["utterances"]), str(plan["voices"]), str(entry["stems"]),
               "--video", str(entry["video"]), "-o", str(entry["output"]),
               "--clips", str(entry["clips"]),
               "--pause-file", str(plan["work"] / PAUSE), *plan["options"]]
    if plan["queue"]:
        command = ["processqueue", plan["queue"], *command]

    child = subprocess.Popen(command)

    def stop(number, frame):
        print(f"\n[season] stop asked for; {entry['stem']} will finish its "
              f"current line and keep what it has drawn", flush=True)
        halt(plan, "stopped by signal")

    previous = [(number, signal.signal(number, stop))
                for number in (signal.SIGINT, signal.SIGTERM)]
    try:
        return child.wait()
    finally:
        for number, handler in previous:
            signal.signal(number, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show", nargs="?",
                        help="which season: a title, any of its aliases, or the "
                             "path to a plan. Omitted means every prepared season, "
                             "which --status and --halt accept and a run does not")
    parser.add_argument("--status", action="store_true",
                        help="print where the season stands and stop")
    parser.add_argument("--halt", action="store_true",
                        help="set the pause file, stopping a run already going")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="render at most N episodes, then stop")
    parser.add_argument("--only", type=int, action="append", metavar="N",
                        help="render only this episode number; repeatable")
    args = parser.parse_args()

    paths = resolve(args.show)
    if not paths:
        raise SystemExit("no season plans under dub/; see docs/dubbing.md")

    if args.status:
        for index, path in enumerate(paths):
            plan = read_plan(path)
            print() if index else None
            report(plan, episodes(plan), path)
        return 0

    if args.halt:
        for path in paths:
            plan = read_plan(path)
            print(f"halted {plan['show']}: {halt(plan, 'halted by hand')}")
        print("a render in flight stops after the line it is on; "
              "nothing already finished is affected")
        return 0

    # Ambiguity is only a problem for the one verb that does something. Status
    # and halt are happy to apply to everything, and usually should.
    if len(paths) > 1:
        shows = ", ".join(read_plan(path)["show"] for path in paths)
        raise SystemExit(f"name which season to render: {shows}")

    plan = read_plan(paths[0])
    found = episodes(plan)
    if not found:
        raise SystemExit(f"no parsed episodes in {plan['work']}")

    # Running the tool is the instruction to run, so it clears the brake. That
    # also means the resume command is the same command as the start, which is
    # the thing a session days later should not have to work out.
    brake = plan["work"] / PAUSE
    if brake.exists():
        print(f"clearing {brake} ({brake.read_text().strip()})")
        brake.unlink()

    pending = [e for e in found if not e["output"].exists()
               and (args.only is None or e["number"] in args.only)]
    if not pending:
        print(f"{plan['show']} season {plan['season']:02d} is fully rendered")
        return 0

    print(f"{len(pending)} episode(s) to render, one at a time; stop any time "
          f"with\n  python3 scripts/dub_season.py \"{plan['show']}\" --halt\n")

    for done, entry in enumerate(pending):
        if args.limit is not None and done >= args.limit:
            print(f"stopping after {done} episode(s), as asked")
            break
        gaps = missing(entry)
        if gaps:
            print(f"skipping {entry['stem']}: missing "
                  + ", ".join(str(path) for path in gaps))
            continue

        state, drawn, lines = progress(entry)
        picking_up = f", resuming from {drawn} of {lines} lines" if drawn else ""
        print(f"\n=== S{plan['season']:02d}E{entry['number']:02d} "
              f"({done + 1} of {len(pending)}){picking_up} ===", flush=True)

        code = render(plan, entry)
        if entry["output"].exists():
            refresh_emby()
        if code == EXIT_PAUSED:
            print(f"\npaused. {plan['show']} resumes exactly here with\n"
                  f"  python3 scripts/dub_season.py \"{plan['show']}\"")
            return 0
        if code != 0:
            print(f"\n{entry['stem']} failed with exit {code}; stopping rather "
                  f"than carrying the same fault through the rest of the season")
            return code

    report(plan, episodes(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
