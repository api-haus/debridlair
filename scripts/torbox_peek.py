#!/usr/bin/env python3
"""Check what's inside a Torbox torrent before committing it to the library.

Prints each file's direct-download URL — the same kind ffprobe/ffmpeg can
read straight over HTTP, so a subtitle track (or anything else) can be
inspected without pulling the whole video to local disk. Useful for candidate
releases you might reject: `torbox_add.py` a magnet, `torbox_peek.py` the
resulting torrent_id to get links, survey what you need, then delete it if it
doesn't pan out (see AGENTS.md's Torbox-deletes rule).

Usage:
    python3 torbox_peek.py TORRENT_ID              # wait for caching, list files
    python3 torbox_peek.py TORRENT_ID --grep S01E01 # only files matching a substring
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from torbox_sync import api_get, load_env, strm_url  # noqa: E402

READY_STATES = ("cached", "completed", "downloaded", "seeding", "uploading")
POLL_SECONDS = 5
POLL_ATTEMPTS = 24  # two minutes


def find_item(torrent_id, key):
    for item in api_get("/torrents/mylist?bypass_cache=true", key):
        if str(item.get("id")) == str(torrent_id):
            return item
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    torrent_id = args[0]
    grep = None
    if "--grep" in sys.argv:
        grep = sys.argv[sys.argv.index("--grep") + 1]

    key = load_env()["TORBOX_API_KEY"]

    item = None
    for attempt in range(POLL_ATTEMPTS):
        item = find_item(torrent_id, key)
        if item is None:
            sys.exit(f"no torrent {torrent_id} in this Torbox account")
        state = (item.get("download_state") or "").lower()
        if state in READY_STATES or item.get("download_present"):
            break
        if attempt == 0:
            print(f"waiting for Torbox to cache it (state={state})...", file=sys.stderr)
        time.sleep(POLL_SECONDS)
    else:
        sys.exit(f"still not cached after {POLL_SECONDS * POLL_ATTEMPTS}s "
                 f"(state={(item.get('download_state') or '').lower()})")

    files = item.get("files") or []
    if grep:
        files = [f for f in files if grep.lower() in f["name"].lower()]
    if not files:
        sys.exit("no matching files" if grep else "torrent has no files listed")

    for f in files:
        print(f"{f['name']}\t{strm_url('torrents', key, torrent_id, f['id'])}")


if __name__ == "__main__":
    main()
