#!/usr/bin/env python3
"""Force Emby to lazy-probe .strm items by requesting PlaybackInfo for each.

Emby only probes .strm media on demand (playback), which leaves episodes
without MediaStreams/runtime — and intro detection needs those. This script
requests PlaybackInfo for every unprobed movie/episode, which makes Emby
probe the remote URL exactly like a real playback start would.

Usage: emby_probe.py [--limit N] [--series "Name"]
Reads the API key from ../sync-state/emby_api_key (falls back to prompting
emby_setup.py's stored credentials are not needed).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BASE_URL = "http://localhost:8096/emby"
CLIENT = ('MediaBrowser Client="debrid-probe", Device="probe", '
          'DeviceId="debrid-probe-1", Version="1.0.0"')
PROFILE = {"DeviceProfile": {"MaxStreamingBitrate": 200000000,
                             "DirectPlayProfiles": [{"Type": "Video"},
                                                    {"Type": "Audio"}]}}


def req(method, path, api_key, body=None, timeout=90):
    url = BASE_URL + path
    sep = "&" if "?" in url else "?"
    url += f"{sep}api_key={api_key}"
    headers = {"X-Emby-Authorization": CLIENT, "Accept": "application/json",
               "User-Agent": "debrid-emby-stack/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max items to probe")
    ap.add_argument("--series", default="", help="only episodes of this series")
    args = ap.parse_args()

    api_key = (BASE / "sync-state" / "emby_api_key").read_text().strip()
    user_id = req("GET", "/Users", api_key)[0]["Id"]

    q = ("Recursive=true&IncludeItemTypes=Episode,Movie&Limit=10000"
         "&Fields=MediaStreams,Path,SeriesName")
    items = req("GET", f"/Items?{q}", api_key)["Items"]
    todo = [i for i in items if not i.get("MediaStreams")]
    if args.series:
        todo = [i for i in todo
                if i.get("SeriesName") == args.series or
                args.series.lower() in (i.get("Path") or "").lower()]
    if args.limit:
        todo = todo[:args.limit]
    print(f"unprobed strm items: {len(todo)}")
    if not todo:
        return

    done = [0]
    t0 = time.time()

    def probe(item):
        try:
            req("POST", f"/Items/{item['Id']}/PlaybackInfo?UserId={user_id}",
                api_key, PROFILE)
            done[0] += 1
            if done[0] % 10 == 0:
                rate = done[0] / (time.time() - t0)
                print(f"  {done[0]}/{len(todo)} ({rate:.1f}/s)", flush=True)
            return True
        except Exception as e:
            print(f"  [warn] {item['Name'][:60]}: {e}", file=sys.stderr)
            return False

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(probe, todo))
    print(f"probed {sum(results)}/{len(todo)} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
