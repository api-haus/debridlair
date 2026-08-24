#!/usr/bin/env python3
"""Report library items whose bitrate exceeds the shared bandwidth cap.

Reads MediaStreams straight from Emby (no re-probing) — a probed item's
video+audio BitRate sums match what ffprobe would say against the same
Torbox CDN URL. An unprobed item (no MediaStreams) falls back to an
estimate from Size / RunTimeTicks and is flagged so the caller knows
emby_probe.py hasn't seen it yet.

Usage: bitrate_audit.py [--cap 15] [--type movie|episode|all] [--limit N]
Prints offenders sorted worst-first: name, Mbit/s, size, torrent_id, path.
"""
import argparse
import json
import re
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BASE_URL = "http://localhost:8096/emby"


def req(api_key, path):
    url = f"{BASE_URL}{path}{'&' if '?' in path else '?'}api_key={api_key}"
    with urllib.request.urlopen(url, timeout=90) as resp:
        return json.loads(resp.read())


def torrent_id(path):
    m = re.search(r"torrent_id=(\d+)", path or "")
    return m.group(1) if m else "-"


def bitrate_bps(source):
    streams = source.get("MediaStreams") or []
    known = [s.get("BitRate") for s in streams
             if s.get("Type") in ("Video", "Audio") and s.get("BitRate")]
    if known:
        return sum(known), True
    size = source.get("Size") or 0
    ticks = source.get("RunTimeTicks") or 0
    if size and ticks:
        seconds = ticks / 10_000_000
        return int(size * 8 / seconds), False
    return 0, False


def find_offenders(api_key, item_types="Movie", cap=15.0):
    """Return (offenders worst-first, total items checked, unprobed count)."""
    q = (f"Recursive=true&IncludeItemTypes={item_types}&Limit=10000"
         "&Fields=MediaSources,Path,SeriesName,ProductionYear,RunTimeTicks")
    items = req(api_key, f"/Items?{q}")["Items"]

    offenders = []
    unprobed = 0
    for item in items:
        sources = item.get("MediaSources") or []
        if not sources:
            continue
        source = sources[0]
        bps, probed = bitrate_bps(source)
        if not probed:
            unprobed += 1
        if bps / 1_000_000 > cap:
            name = item.get("Name")
            if item.get("SeriesName"):
                name = f"{item['SeriesName']} - {name}"
            offenders.append({
                "id": item.get("Id"),
                "name": name,
                "title": item.get("Name"),
                "year": item.get("ProductionYear"),
                "run_time_ticks": item.get("RunTimeTicks") or source.get("RunTimeTicks"),
                "mbit": bps / 1_000_000,
                "gb": (source.get("Size") or 0) / 1e9,
                "torrent_id": torrent_id(source.get("Path")),
                "path": source.get("Path"),
                "probed": probed,
            })

    offenders.sort(key=lambda o: -o["mbit"])
    return offenders, len(items), unprobed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=15.0, help="Mbit/s ceiling")
    ap.add_argument("--type", default="movie", choices=["movie", "episode", "all"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    api_key = (BASE / "sync-state" / "emby_api_key").read_text().strip()
    types = {"movie": "Movie", "episode": "Episode", "all": "Movie,Episode"}[args.type]
    offenders, total, unprobed = find_offenders(api_key, types, args.cap)
    if args.limit:
        offenders = offenders[:args.limit]

    print(f"{total} items checked, {unprobed} unprobed (estimated from size/runtime), "
          f"{len(offenders)} over {args.cap} Mbit/s")
    for o in offenders:
        flag = "" if o["probed"] else " (est.)"
        print(f"  {o['mbit']:6.1f} Mbit/s{flag}  {o['gb']:5.1f} GB  "
              f"id={o['torrent_id']:>9}  {o['name']}")


if __name__ == "__main__":
    main()
