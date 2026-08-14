#!/usr/bin/env python3
"""Fetch missing subtitles for library items through Emby's own providers.

PREFS says English subtitles are the default and stay on, but `torbox_find.py`
ranks releases on resolution and seeders and never checks subtitles — so a
winning release routinely arrives with none, and small or old titles have no
better release to swap to. This asks Emby's configured providers (Open
Subtitles) for the missing language, picks the candidate that best matches the
release on disk, downloads it beside the .strm and refreshes the item.

The sidecar is safe there: `torbox_sync.py` only ever prunes `*.strm`, and it
removes a directory only when empty.

Usage: emby_subs.py [--lang eng] [--title "Name"] [--limit N] [--dry-run]
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BASE_URL = "http://localhost:8096/emby"
CLIENT = ('MediaBrowser Client="debrid-subs", Device="subs", '
          'DeviceId="debrid-subs-1", Version="1.0.0"')
# Emby wants 3-letter codes on the search path but reports 2-letter on streams.
ALIAS = {"eng": {"eng", "en", "english"}, "kor": {"kor", "ko", "korean"},
         "jpn": {"jpn", "ja", "japanese"}, "spa": {"spa", "es", "spanish"},
         "fre": {"fre", "fra", "fr", "french"}, "ger": {"ger", "de", "german"}}
SPLIT = re.compile(r"\b(?:cd|disc|disk|part|pt)\s*[.\-_]?\s*([0-9])\b", re.I)
TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = {"the", "a", "an", "of", "and", "or"}


def req(method, path, api_key, body=None, timeout=90):
    url = BASE_URL + path
    url += ("&" if "?" in url else "?") + f"api_key={api_key}"
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


def has_language(item, lang):
    names = ALIAS.get(lang, {lang})
    for s in item.get("MediaStreams") or []:
        if s.get("Type") != "Subtitle":
            continue
        tag = (s.get("Language") or s.get("DisplayLanguage") or "").lower()
        if tag in names:
            return True
    return False


def tokens(text):
    return set(TOKEN.findall(text.lower()))


def score(cand, release, title):
    """Rank a candidate against the release on disk; negative means reject."""
    name = cand.get("Name") or ""
    # A CD-split subtitle covers half a runtime and silently desyncs the rest.
    if SPLIT.search(name) and not SPLIT.search(release):
        return -1
    # Shared words alone married "DEVILMAN NIGHT ... LIVE" to Saturday Night
    # Live. Wrong subtitles are worse than none, so the title has to carry.
    want = {t for t in tokens(title) if t not in STOPWORDS}
    covered = len(want & tokens(name)) / len(want) if want else 0.0
    if not cand.get("IsHashMatch") and covered < 0.6:
        return -1
    shared = tokens(name) & tokens(release)
    return (200 if cand.get("IsHashMatch") else 0) + 100 * covered \
        + 10 * len(shared) + min(cand.get("DownloadCount") or 0, 5000) / 1000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="eng", help="3-letter code (default eng)")
    ap.add_argument("--title", default="", help="substring of the item name")
    ap.add_argument("--id", default="", help="comma-separated Emby item ids")
    ap.add_argument("--limit", type=int, default=0, help="max items to fix")
    ap.add_argument("--dry-run", action="store_true", help="show, do not fetch")
    args = ap.parse_args()

    api_key = (BASE / "sync-state" / "emby_api_key").read_text().strip()
    q = ("Recursive=true&IncludeItemTypes=Episode,Movie&Limit=10000"
         "&Fields=MediaStreams,Path,SeriesName,ProductionYear")
    items = req("GET", f"/Items?{q}", api_key)["Items"]

    # No streams at all means unprobed, not subtitle-less — emby_probe.py first.
    unprobed = [i for i in items if not i.get("MediaStreams")]
    todo = [i for i in items
            if i.get("MediaStreams") and not has_language(i, args.lang)]
    if args.id:
        wanted = {s.strip() for s in args.id.split(",") if s.strip()}
        todo = [i for i in todo if i["Id"] in wanted]
    if args.title:
        needle = args.title.lower()
        todo = [i for i in todo
                if needle in (i.get("Name") or "").lower()
                or needle in (i.get("SeriesName") or "").lower()]
    todo.sort(key=lambda i: (i.get("SeriesName") or "", i.get("Name") or ""))
    if args.limit:
        todo = todo[:args.limit]

    if unprobed:
        print(f"[note] {len(unprobed)} unprobed items skipped; "
              f"run emby_probe.py to include them", file=sys.stderr)
    print(f"missing {args.lang} subtitles: {len(todo)}")

    fixed = failed = 0
    for item in todo:
        label = item.get("Name") or item["Id"]
        if item.get("SeriesName"):
            label = f"{item['SeriesName']} - {label}"
        release = Path(item.get("Path") or "").name
        try:
            found = req("GET",
                        f"/Items/{item['Id']}/RemoteSearch/Subtitles/{args.lang}",
                        api_key)
        except urllib.error.HTTPError as e:
            print(f"  [warn] {label}: search failed ({e.code})", file=sys.stderr)
            failed += 1
            continue
        ranked = sorted(((score(c, release, item.get("Name") or ""), c)
                         for c in found), key=lambda p: p[0], reverse=True)
        ranked = [(s, c) for s, c in ranked if s >= 0]
        if not ranked:
            print(f"  [none] {label}")
            failed += 1
            continue
        best = ranked[0][1]
        if args.dry_run:
            print(f"  [would] {label} <- {best['Name'][:58]} "
                  f"({best.get('ProviderName')})")
            continue
        try:
            req("POST", f"/Items/{item['Id']}/RemoteSearch/Subtitles/"
                        f"{urllib.parse.quote(best['Id'])}", api_key)
        except urllib.error.HTTPError as e:
            print(f"  [warn] {label}: download failed ({e.code})",
                  file=sys.stderr)
            failed += 1
            continue
        print(f"  [ok]   {label} <- {best['Name'][:58]}")
        fixed += 1
        # The file is already on disk; a refresh that 400s costs only the scan.
        try:
            req("POST", f"/Items/{item['Id']}/Refresh?MetadataRefreshMode="
                        f"FullRefresh&ImageRefreshMode=Default"
                        f"&ReplaceAllMetadata=false", api_key)
        except urllib.error.HTTPError as e:
            print(f"  [warn] {label}: refresh failed ({e.code}), rescan later",
                  file=sys.stderr)
        # Open Subtitles rate-limits hard; a burst gets the whole run banned.
        time.sleep(1.0)

    if not args.dry_run:
        print(f"fetched {fixed}, unresolved {failed}")


if __name__ == "__main__":
    main()
