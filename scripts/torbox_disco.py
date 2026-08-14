#!/usr/bin/env python3
"""Queue every album an indexer holds for one artist, one release per album.

Usage:
    python3 torbox_disco.py "David S. Ware"            # queue what is missing
    python3 torbox_disco.py "Shibusashirazu" --list    # show the plan only
    python3 torbox_disco.py "Shibusashirazu" "Shibusa Shirazu"   # both spellings
    python3 torbox_disco.py "Artist" --allow-lossy     # accept MP3 and friends
    python3 torbox_disco.py --drain                    # queue what now fits

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

Torbox runs only a plan's worth of downloads at once. A burst past that is
accepted, dropped, and answered with a day-long account cooldown — 27 of 31
albums vanished that way. So whatever does not fit is written to
`sync-state/album_queue.tsv`, and `--drain` (hourly, from the torbox-sync
loop) queues the next few whenever slots come free.
"""
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import urllib.parse  # noqa: E402
from torbox_find import PROWLARR, music_score, prowlarr_key  # noqa: E402
from torbox_find import queue, search  # noqa: E402
from torbox_sync import BASE, LIB_MUSIC, api_get, load_env  # noqa: E402
from torbox_sync import split_artist_album  # noqa: E402

# A compilation credited to nobody: one track of the artist, twelve of others
VARIOUS = re.compile(r"^(va|various)\b", re.I)
WANTLIST = BASE / "sync-state" / "album_queue.tsv"
# Torbox runs a plan's worth of downloads at once and puts the account in a
# day-long cooldown when a burst overruns it, dropping the excess adds
# silently. Slots for plan 2; --slots overrides.
PLAN_SLOTS = {1: 3, 2: 5, 3: 10}
BUSY = ("downloading", "checking", "queued")


def key(name):
    # \w, not a-z: a query in kana must not normalize to the empty string,
    # which is a substring of every title and matches the whole indexer
    return re.sub(r"[^\w]+", "", (name or "").lower())


def free_slots(override=None):
    """How many uncached torrents Torbox will take right now."""
    tb = load_env()["TORBOX_API_KEY"]
    me = api_get("/user/me", tb)
    until = me.get("cooldown_until")
    if until:
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if end > datetime.now(timezone.utc):
            print(f"torbox is in cooldown until {until}; queuing nothing",
                  file=sys.stderr)
            return 0
    slots = override or PLAN_SLOTS.get(me.get("plan"), 3)
    slots += me.get("additional_concurrent_slots") or 0
    busy = sum(1 for i in api_get("/torrents/mylist?bypass_cache=true", tb)
               if (i.get("download_state") or "").lower().startswith(BUSY))
    return max(0, slots - busy)


def pack_url(url):
    """Store a grab link without the host or the Prowlarr key."""
    if not url or url.startswith("magnet:"):
        return url or ""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "apikey"]
    return urllib.parse.urlunsplit(("", "", u.path,
                                    urllib.parse.urlencode(q), ""))


def unpack_url(stored):
    """Rebuild it against this caller's Prowlarr — the host differs in the
    container, and the key never belonged in a file."""
    if stored.startswith("magnet:"):
        return stored
    sep = "&" if "?" in stored else "?"
    return f"{PROWLARR}{stored}{sep}apikey={prowlarr_key()}"


def read_wantlist():
    if not WANTLIST.exists():
        return []
    rows = [ln.split("\t") for ln in WANTLIST.read_text().splitlines() if ln]
    return [r for r in rows if len(r) == 5]


def write_wantlist(rows):
    WANTLIST.parent.mkdir(parents=True, exist_ok=True)
    WANTLIST.write_text("".join("\t".join(r) + "\n" for r in rows))


def slots_override(flags):
    for f in flags:
        if f.startswith("--slots="):
            return int(f.split("=", 1)[1])
    return None


def drain(rows, slots):
    """Queue what fits in the free slots; return the rows that did not."""
    if slots <= 0:
        print(f"{len(rows)} albums held in {WANTLIST.name} for the next pass")
        return rows
    held = held_albums()
    sent = 0
    for i, row in enumerate(rows):
        if sent >= slots:
            print(f"queued {sent}; {len(rows) - i} held in {WANTLIST.name}")
            return rows[i:]
        album, _year, size, title, url = row
        if key(album) in held:
            continue          # it landed since the row was written
        try:
            queue({"title": title, "size": int(size or 0), "seeders": "?",
                   "downloadUrl": unpack_url(url)})
            sent += 1
        except Exception as e:
            # A stale Prowlarr link: re-running the artist finds it again
            print(f"  [warn] dropped {album}: {e}", file=sys.stderr)
        time.sleep(2)
    print(f"queued {sent}; album queue is empty")
    return []


def held_albums():
    """Normalized album names already in the library, per artist directory."""
    return {key(re.sub(r"\s*\((?:19|20)\d{2}\)$", "", d.name))
            for artist in LIB_MUSIC.glob("*")
            for d in artist.glob("*") if d.is_dir()}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    if "--drain" in flags:
        rows = read_wantlist()
        if not rows:
            print("album queue is empty")
            return
        write_wantlist(drain(rows, free_slots(slots_override(flags))))
        return
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

    rows = [[album, str(year or ""), str(r.get("size") or 0),
             r.get("title") or "",
             pack_url(r.get("magnetUrl") or r.get("downloadUrl"))]
            for album, year, r in plan]
    pending = read_wantlist()
    known = {row[3] for row in pending}
    rows = pending + [r for r in rows if r[3] not in known]
    write_wantlist(drain(rows, free_slots(slots_override(flags))))


if __name__ == "__main__":
    main()
