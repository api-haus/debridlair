#!/usr/bin/env python3
"""Queue newly aired episodes for the shows listed in sync-state/watchlist.txt.

Each non-comment line is:

    <library/tv folder> | <query template>

The template gets the next episode number substituted for {ep} ({ep:02d} to
zero-pad) and the season for {season}:

    Chainsmoker Cat | Chainsmoker Cat S01E{ep:02d}

The watcher reads the highest episode already present in that folder's newest
season and searches for the ones after it, so it never re-queues what the
library already has. It gives up after MISS_LIMIT consecutive episodes return
nothing, which is what makes a finished season cost only two searches a run
rather than one per remaining slot.

Language and streamability policy is not reimplemented here: releases come
from torbox_find.search(), which already refuses remuxes, over-cap sizes and
dub-only releases.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import torbox_find  # noqa: E402
from torbox_sync import BASE, EP_TOKEN, LIB_TV  # noqa: E402

WATCHLIST = BASE / "sync-state" / "watchlist.txt"
# Consecutive episodes that must come back empty before we accept that the
# season has simply not aired any further.
MISS_LIMIT = 2
# Hard stop, so a mis-typed template can't walk the indexers indefinitely.
LOOKAHEAD = 12


def latest(show_dir):
    """Return (season, highest episode) for the newest season, or None."""
    seasons = sorted(d for d in show_dir.iterdir()
                     if d.is_dir() and d.name.lower().startswith("season"))
    if not seasons:
        return None
    found = []
    for f in seasons[-1].glob("*.strm"):
        m = EP_TOKEN.search(f.name)
        if m:
            found.append((int(m.group(1)), int(m.group(2))))
    if not found:
        return None
    season = max(s for s, _ in found)
    return season, max(e for s, e in found if s == season)


def main():
    if not WATCHLIST.exists():
        sys.exit(f"no watchlist at {WATCHLIST}")
    key = torbox_find.prowlarr_key()
    for line in WATCHLIST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        folder, _, template = line.partition("|")
        folder, template = folder.strip(), template.strip()
        show_dir = LIB_TV / folder
        if not template:
            print(f"[skip] {folder}: no query template", file=sys.stderr)
            continue
        if not show_dir.is_dir():
            print(f"[skip] {folder}: not in the library yet", file=sys.stderr)
            continue
        current = latest(show_dir)
        if not current:
            print(f"[skip] {folder}: no episode numbers to count from",
                  file=sys.stderr)
            continue
        season, have = current
        misses = queued = 0
        ep = have
        while misses < MISS_LIMIT and ep - have < LOOKAHEAD:
            ep += 1
            try:
                results, _ = torbox_find.search(
                    template.format(ep=ep, season=season), tv=True, key=key)
            except Exception as e:
                # One flaky indexer shouldn't abort the rest of the watchlist.
                print(f"[warn] {folder} E{ep:02d}: {e}", file=sys.stderr)
                break
            if not results:
                misses += 1
                continue
            misses = 0
            queued += 1
            torbox_find.queue(results[0])
        # A finished season would otherwise sit here forever finding nothing,
        # so also look for the next season premiering. Only E01 — once that
        # lands it becomes the newest season and the loop above takes over.
        # Skipped for templates that hard-code their season, where this would
        # just re-search the current season's E01.
        if "{season" in template:
            try:
                nxt, _ = torbox_find.search(
                    template.format(ep=1, season=season + 1), tv=True, key=key)
            except Exception as e:
                print(f"[warn] {folder} S{season + 1:02d}E01: {e}",
                      file=sys.stderr)
                nxt = []
            if nxt:
                queued += 1
                torbox_find.queue(nxt[0])
        print(f"watch: {folder} S{season:02d} had E{have:02d}, "
              f"queued {queued} new")


if __name__ == "__main__":
    main()
