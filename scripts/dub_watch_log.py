#!/usr/bin/env python3
"""Report only the episodes worth interrupting somebody about.

A season is fifty renders and most of them are unremarkable. Announcing each
one trains whoever is reading to skim, which is the state you do not want them
in on the render that actually went wrong. So this reads a season log and stays
silent through a normal episode.

An episode is worth a word when it is genuinely tight — measured across a
season, the share of lines that had to be compressed predicts the overruns far
better than the line count does — or when something failed, paused, or
finished. Everything else is a number you can ask for at any time with
`dub_season.py --quality`.

Usage:
    tail -f -n0 dub/work/season.log | python3 scripts/dub_watch_log.py
"""

import argparse
import io
import re
import sys

# Above this share of lines compressed, an episode is dense enough that the
# overruns stop being incidental. Around a quarter is comfortable and lands a
# handful; a third and over has landed fifteen to twenty.
TIGHT = 0.33

# Or this many lines still overrunning after compression, whatever the share.
OVERRUNS = 12

PLACED = re.compile(r"(\d+) lines placed, (\d+) compressed to fit, (\d+) still overrunning")
EPISODE = re.compile(r"^=== (S\d+E\d+)")

# Things that always deserve a word, whatever the numbers say.
LOUD = re.compile(r"failed with exit|^Traceback|^paused\.|is fully rendered"
                  r"|^skipping |not resolved yet|IGNORED \d+ rewrites")

# A drifted character, and how many of its lines the drift was measured over.
DRIFT = re.compile(r"^(\S+)\s+(\d+)Hz\s+(\d+)Hz\s+(-?\d+)%\s+(\d+)\s+<- drifted")

# Below this many measured lines a drift is not a finding. The median of two
# readings is not a measurement, and a walk-on with two lines drifts loudly and
# harmlessly all season — one character read -6% over five lines in one episode
# and +84% over two in another, from the same reference and the same bank.
DRIFT_LINES = 4


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ignore-drift", default="", metavar="NAMES",
                        help="comma-separated speakers whose drift is not a fault "
                             "and never will be — a fansub's generic label for "
                             "walk-ons ('GUY', 'KID') is one banked voice standing "
                             "in for many different people, so it drifts against "
                             "its own reference every episode and re-minting it "
                             "cannot help, because there is no one voice to mint")
    args = parser.parse_args()
    forgiven = {n.strip().upper() for n in args.ignore_drift.split(",") if n.strip()}

    # A render log is not clean text. Model progress bars redraw with control
    # bytes and a line can be torn mid-character by a partial write, so strict
    # decoding stops the watch on the first one — which is silence exactly when
    # something is being logged. Undecodable bytes are not worth dying over.
    stream = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

    episode = "?"
    for line in stream:
        line = line.rstrip("\n").replace("\0", "")

        seen = EPISODE.search(line)
        if seen:
            episode = seen.group(1)
            continue

        if LOUD.search(line):
            print(f"{episode}: {line.strip()}", flush=True)
            continue

        drifted = DRIFT.search(line)
        if drifted:
            who, ref, dub, off, lines = drifted.groups()
            if int(lines) >= DRIFT_LINES and who.upper() not in forgiven:
                print(f"{episode}: {who} drifted {off}% ({ref}Hz reference, "
                      f"{dub}Hz in the dub) over {lines} lines — re-mint its "
                      f"reference, or suspect the fitting rather than the clone",
                      flush=True)
            continue

        placed = PLACED.search(line)
        if not placed:
            continue
        lines, squeezed, over = (int(n) for n in placed.groups())
        share = squeezed / lines if lines else 0
        if share >= TIGHT or over >= OVERRUNS:
            print(f"{episode} is tight: {share * 100:.0f}% of {lines} lines "
                  f"compressed, {over} still overrunning — worth a listen, and "
                  f"dub_adapt.py rather than a re-render", flush=True)


if __name__ == "__main__":
    sys.exit(main())
