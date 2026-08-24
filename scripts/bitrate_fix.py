#!/usr/bin/env python3
"""Requeue over-cap movies/episodes with a release whose bitrate actually fits.

Usage:
    python3 bitrate_fix.py                  # movies, target 10-15 Mbit/s
    python3 bitrate_fix.py --type all       # movies + episodes
    python3 bitrate_fix.py --lo 8 --hi 15
    python3 bitrate_fix.py --drain          # resume from bitrate_queue.tsv
    python3 bitrate_fix.py --dry-run

For each offender from bitrate_audit.find_offenders(), searches 1080p (then
plain) releases, estimates each candidate's bitrate from its size against the
item's own RunTimeTicks (release search results carry no runtime), and keeps
the best-scoring candidate whose estimate lands in [lo, hi]. Queues it,
deletes the superseded torrent immediately — before the next torbox_sync.py
runs — because the sync's quality dedupe scores by resolution and would
otherwise drop the new, lower-resolution file and keep the old one.

Torbox runs only a plan's worth of concurrent downloads; a burst past that is
silently dropped and costs a day-long cooldown (see torbox_disco.py). This
checks free_slots() before every queue and, once slots run out, writes
whatever's left to sync-state/bitrate_queue.tsv for --drain to pick up later.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bitrate_audit import find_offenders  # noqa: E402
from torbox_disco import free_slots  # noqa: E402
from torbox_find import queue, resolve_grab, score  # noqa: E402
from torbox_find import search as tb_search  # noqa: E402
from torbox_sync import BASE, api_get, load_env  # noqa: E402

QUEUE_FILE = BASE / "sync-state" / "bitrate_queue.tsv"


def clean_title(t):
    return re.sub(r"[:\-–—'\"!?,.]", " ", t or "").strip()


def candidates(item, lo, hi):
    """Yield (release, estimated_mbit) across query variants, in-range only."""
    ticks = item.get("run_time_ticks") or 0
    seconds = ticks / 10_000_000 if ticks else 0
    if not seconds:
        return
    title = clean_title(item["title"])
    year = item.get("year") or ""
    queries = [f"{title} {year} 1080p", f"{title} {year}"]
    seen = set()
    for q in queries:
        try:
            results, _ = tb_search(q, tv=False, allow_fat=True)
        except SystemExit:
            continue
        for r in results:
            key = r.get("magnetUrl") or r.get("downloadUrl")
            if key in seen:
                continue
            seen.add(key)
            size = r.get("size") or 0
            if not size:
                continue
            mbit = size * 8 / seconds / 1_000_000
            if lo <= mbit <= hi:
                yield r, mbit


def best_candidate(item, lo, hi):
    pool = list(candidates(item, lo, hi))
    if not pool:
        return None, None
    pool.sort(key=lambda p: score(p[0]), reverse=True)
    return pool[0]


def queue_and_get_id(r):
    """Like torbox_find.queue(), but returns the new torrent_id."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from torbox_add import post_form  # noqa: E402
    grab = resolve_grab(r.get("magnetUrl") or r.get("downloadUrl"))
    key = load_env()["TORBOX_API_KEY"]
    fields = {"token": key, "seed": "1", "allow_zip": "false"}
    files = None
    if grab.startswith("magnet:"):
        fields["magnet"] = grab
    else:
        files = {"file": (Path(grab).name, Path(grab).read_bytes())}
    resp = post_form("/torrents/createtorrent", fields, files)
    if not resp.get("success"):
        raise RuntimeError(f"{resp.get('error')} - {resp.get('detail')}")
    return (resp.get("data") or {}).get("torrent_id")


def delete_torrent(torrent_id):
    key = load_env()["TORBOX_API_KEY"]
    import json
    import urllib.request
    req = urllib.request.Request(
        "https://api.torbox.app/v1/api/torrents/controltorrent",
        data=json.dumps({"torrent_id": int(torrent_id), "operation": "delete"}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def read_queue():
    if not QUEUE_FILE.exists():
        return []
    rows = [ln.split("\t") for ln in QUEUE_FILE.read_text().splitlines() if ln]
    return [{"id": r[0], "title": r[1], "year": r[2] or None,
             "run_time_ticks": int(r[3]) if r[3] else 0,
             "torrent_id": r[4], "mbit": float(r[5])}
            for r in rows if len(r) == 6]


def write_queue(items):
    lines = [f"{i['id']}\t{i['title']}\t{i.get('year') or ''}\t"
             f"{i.get('run_time_ticks') or 0}\t{i['torrent_id']}\t{i['mbit']:.1f}"
             for i in items]
    QUEUE_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


def main():
    args = sys.argv[1:]
    lo = float(args[args.index("--lo") + 1]) if "--lo" in args else 10.0
    hi = float(args[args.index("--hi") + 1]) if "--hi" in args else 15.0
    item_type = args[args.index("--type") + 1] if "--type" in args else "movie"
    dry_run = "--dry-run" in args
    drain = "--drain" in args

    api_key = (BASE / "sync-state" / "emby_api_key").read_text().strip()
    types = {"movie": "Movie", "episode": "Episode", "all": "Movie,Episode"}[item_type]

    if drain:
        pending = read_queue()
        print(f"draining {len(pending)} queued from a prior run")
    else:
        offenders, total, unprobed = find_offenders(api_key, types, hi)
        pending = read_queue()
        seen_ids = {p["id"] for p in pending}
        pending += [o for o in offenders if o["id"] not in seen_ids]
        print(f"{total} items checked, {unprobed} unprobed, "
              f"{len(offenders)} over {hi} Mbit/s, {len(pending)} total pending")

    queued, no_fit, deferred = [], [], []
    for item in pending:
        if free_slots() <= 0:
            deferred.append(item)
            continue
        r, mbit = best_candidate(item, lo, hi)
        if not r:
            no_fit.append(item)
            continue
        print(f"{item['name']}: {item['mbit']:.1f} -> ~{mbit:.1f} Mbit/s "
              f"| {r.get('title')} ({(r.get('size') or 0)/1e9:.1f} GB, {r.get('seeders')} seeds)")
        if dry_run:
            queued.append(item)
            continue
        try:
            new_id = queue_and_get_id(r)
            delete_torrent(item["torrent_id"])
            print(f"  queued id={new_id}, deleted old id={item['torrent_id']}")
            queued.append(item)
        except Exception as e:
            print(f"  [warn] {item['name']}: {e}", file=sys.stderr)
            deferred.append(item)
        time.sleep(3)

    write_queue(deferred)
    print(f"\nqueued {len(queued)}, no in-range release found for {len(no_fit)}, "
          f"deferred {len(deferred)} (slots/errors, written to {QUEUE_FILE.name})")
    if no_fit:
        print("no fit:")
        for i in no_fit:
            print(f"  {i['name']} (still {i['mbit']:.1f} Mbit/s)")


if __name__ == "__main__":
    main()
