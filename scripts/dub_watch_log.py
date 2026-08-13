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
                  r"|^skipping |<- drifted|not resolved yet|IGNORED \d+ rewrites")


def main():
    episode = "?"
    for line in sys.stdin:
        line = line.rstrip("\n").replace("\0", "")

        seen = EPISODE.search(line)
        if seen:
            episode = seen.group(1)
            continue

        if LOUD.search(line):
            print(f"{episode}: {line.strip()}", flush=True)
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
