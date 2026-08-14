#!/usr/bin/env python3
"""Queue every album an indexer holds for one artist, one release per album.

Usage:
    python3 torbox_disco.py "David S. Ware"            # queue what is missing
    python3 torbox_disco.py "Shibusashirazu" --list    # show the plan only
    python3 torbox_disco.py "Shibusashirazu" "Shibusa Shirazu"   # both spellings
    python3 torbox_disco.py "Artist" --allow-lossy     # accept MP3 and friends

A search for an artist returns the same album several times over — different
rips, years and editions. This keeps the best-ranked release per album, drops
what `library/music` already holds, and queues the rest. Ranking, the lossless
gate and the size ceilings are `torbox_find.py`'s; the album name comes from
`torbox_sync.py`, so it matches the folder the sync will build.

Give every spelling an artist circulates under — a tracker that indexes one
will not always index the other, and the results are merged.

**A release only counts if its title carries the artist name.** An indexer
that matches nothing answers with whatever is popular instead of an empty
list, and a query in the wrong script once came back as 82 audiobooks.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from torbox_find import music_score, queue, search  # noqa: E402
from torbox_sync import LIB_MUSIC, split_artist_album  # noqa: E402

# A compilation credited to nobody: one track of the artist, twelve of others
VARIOUS = re.compile(r"^(va|various)\b", re.I)


def key(name):
    # \w, not a-z: a query in kana must not normalize to the empty string,
    # which is a substring of every title and matches the whole indexer
    return re.sub(r"[^\w]+", "", (name or "").lower())


def held_albums():
    """Normalized album names already in the library, per artist directory."""
    return {key(re.sub(r"\s*\((?:19|20)\d{2}\)$", "", d.name))
            for artist in LIB_MUSIC.glob("*")
            for d in artist.glob("*") if d.is_dir()}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    if not args:
        sys.exit(__doc__)
    artist = args[0]
    results, fat, seen_titles, off = [], 0, set(), 0
    for query in args:
        hits, skipped = search(query, music=True,
                               allow_lossy="--allow-lossy" in flags)
        fat += len(skipped)
        for r in hits:
            title = r.get("title") or ""
            if key(query) not in key(title):
                off += 1
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)
            results.append(r)
    if fat:
        print(f"skipped {fat} lossy or over-limit releases", file=sys.stderr)
    if off:
        print(f"ignored {off} results that do not name the artist",
              file=sys.stderr)
    if not results:
        sys.exit(f"nothing lossless indexed for {artist!r}")
    results.sort(key=music_score, reverse=True)   # one order across queries

    held = held_albums()
    best = {}
    for r in results:
        title = r.get("title") or ""
        if title.rstrip().endswith("..."):
            continue          # an indexer's truncated title: the name is a guess
        who, album, year = split_artist_album(title)
        if not album or VARIOUS.match(who or ""):
            continue
        if not (r.get("seeders") or 0):
            continue          # nothing to cache from, however good the name
        if year is None and len(album) <= 6:
            continue          # neither a year nor a name: a title cut short
        k = key(album)
        if not k or k in best:
            continue          # results arrive best-first, so the first wins
        # The same album also arrives with the line-up glued to the front
        # ("...New Quartet Theatre Garonne"); the tail is the album name
        if any(k.endswith(seen) or seen.endswith(k)
               for seen in best if min(len(k), len(seen)) >= 8):
            continue
        best[k] = (album, year, r)

    plan = [(a, y, r) for k, (a, y, r) in best.items()
            if not any(k.endswith(h) or h.endswith(k) for h in held
                       if min(len(k), len(h)) >= 8) and k not in held]
    skip = len(best) - len(plan)
    print(f"{artist}: {len(best)} albums indexed, {skip} already held, "
          f"{len(plan)} to queue")
    for album, year, r in sorted(plan, key=lambda p: p[1] or 0):
        print(f"  {year or '????'}  {album}  "
              f"[{(r.get('size') or 0)/1e9:.2f} GB, {r.get('seeders')} seeds]")
    if "--list" in flags or not plan:
        return

    queued = failed = 0
    for album, _year, r in plan:
        try:
            queue(r)
            queued += 1
        except Exception as e:
            print(f"  [warn] {album}: {e}", file=sys.stderr)
            failed += 1
        time.sleep(1)
    print(f"queued {queued} albums, {failed} failed")


if __name__ == "__main__":
    main()
