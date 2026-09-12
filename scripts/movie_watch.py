#!/usr/bin/env python3
"""Queue a movie from sync-state/movie_watchlist.txt as soon as a release at
webrip-or-better quality appears.

Each non-comment line is one torbox_find.py query, movie categories unless
the line ends in ` --tv` (a season premiere that hasn't aired yet):

    Resident Evil 2026
    Slow Horses S06E01 --tv

A CAM/TELESYNC/R5/screener/workprint leaks days before any real release and
would otherwise win torbox_find's auto-pick on seeders alone (confirmed: a
TELESYSNC of a day-old release outranked every legitimate copy). This refuses
that tier and leaves the line in place until something better is indexed,
so the title never gets stuck on a camrip.

Size/streamability/language policy is not reimplemented here: releases come
from torbox_find.search(), which already enforces all of that. Queuing goes
through the same free-slot check as torbox_disco.py --drain, so a burst of
premieres can't trigger the Torbox cooldown; a cached release still queues
regardless, since a cached add costs no slot.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import torbox_find  # noqa: E402
from torbox_disco import free_slots  # noqa: E402
from torbox_sync import BASE  # noqa: E402

WATCHLIST = BASE / "sync-state" / "movie_watchlist.txt"
LOW_TIER = re.compile(
    r"\b(cam|hdcam|telesync|hdts|\.ts\.|r5|scr(eener)?|workprint)\b", re.I)


def read():
    if not WATCHLIST.exists():
        return []
    return [ln.strip() for ln in WATCHLIST.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def write(queries):
    WATCHLIST.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST.write_text("".join(q + "\n" for q in queries))


def main():
    queries = read()
    if not queries:
        print("movie watchlist empty")
        return
    key = torbox_find.prowlarr_key()
    remaining = []
    for line in queries:
        tv = line.endswith(" --tv")
        query = line[: -len(" --tv")] if tv else line
        try:
            results, _ = torbox_find.search(query, tv=tv, key=key)
        except Exception as e:
            print(f"[warn] {query}: {e}", file=sys.stderr)
            remaining.append(line)
            continue
        good = [r for r in results
                if not LOW_TIER.search(r.get("title") or "")]
        if not good:
            remaining.append(line)
            continue
        pick = good[0]
        if not pick.get("cached") and free_slots() <= 0:
            remaining.append(line)
            continue
        torbox_find.queue(pick)
        print(f"watch: {query} -> queued")
    write(remaining)


if __name__ == "__main__":
    main()
