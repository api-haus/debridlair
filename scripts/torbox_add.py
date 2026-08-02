#!/usr/bin/env python3
"""Queue a new download in Torbox (magnet link or .torrent file).

Usage:
    python3 torbox_add.py "magnet:?xt=urn:btih:..."
    python3 torbox_add.py /path/to/file.torrent

Once Torbox caches it, torbox_sync.py (runs every 15 min in the torbox-sync
container) picks it up, writes the .strm files, Emby scans them in, and the
probe loop forces stream probing — no manual steps.
"""
import json
import sys
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from torbox_sync import load_env, API  # noqa: E402


def post_form(path, fields, files=None):
    boundary = "----debridemby"
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; " \
                f'name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (fname, data) in (files or {}).items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; " \
                f'name="{k}"; filename="{fname}"\r\n' \
                f"Content-Type: application/x-bittorrent\r\n\r\n".encode() \
                + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API + path, data=body,
        headers={"Authorization": f"Bearer {fields['token']}",
                 "User-Agent": "debrid-emby-stack/1.0",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    key = load_env()["TORBOX_API_KEY"]
    arg = sys.argv[1]
    fields = {"token": key, "seed": "1", "allow_zip": "false"}
    files = None
    if arg.startswith("magnet:"):
        fields["magnet"] = arg
    elif Path(arg).is_file():
        files = {"file": (Path(arg).name, Path(arg).read_bytes())}
    else:
        sys.exit("error: argument must be a magnet: URI or a .torrent file")
    r = post_form("/torrents/createtorrent", fields, files)
    if r.get("success"):
        d = r.get("data") or {}
        print(f"queued: {d.get('name')} (id={d.get('torrent_id')}, "
              f"state={d.get('download_state')})")
        print("It will appear in Emby after the next sync cycle (~15 min).")
    else:
        sys.exit(f"Torbox error: {r.get('error')} - {r.get('detail')}")


if __name__ == "__main__":
    main()
