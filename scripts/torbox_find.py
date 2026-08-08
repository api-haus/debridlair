#!/usr/bin/env python3
"""Search indexers (via Prowlarr) and queue the best release into Torbox.

Usage:
    python3 torbox_find.py "Sinners 2025"             # movie, auto-pick best
    python3 torbox_find.py "The Pitt S01" --tv        # TV search categories
    python3 torbox_find.py "Sinners 2025" --list      # show top 10, pick with -n
    python3 torbox_find.py "Sinners 2025" -n 3        # queue result #3
    python3 torbox_find.py "Sinners 2025" --allow-fat # include over-cap releases

Releases that can never stream within the 40 Mbit emby throttle (remuxes,
movies >30 GB, episodes >12 GB, packs >80 GB) are refused by default.

After queuing, the torbox-sync loop (15 min) writes the .strm files and Emby
picks the title up automatically.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from torbox_sync import load_env  # noqa: E402

# Overridable because callers inside the compose network reach Prowlarr by
# service name, not on the host's published port.
PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696")
TV_CATS = "5000,5010,5020,5030,5040,5045,5050"      # TV + HD/SD/UHD
MOVIE_CATS = "2000,2010,2020,2030,2040,2045,2050,2060"  # Movies


def resolve_grab(url):
    """Resolve a Prowlarr download URL to a magnet URI or a .torrent path."""
    if url.startswith("magnet:"):
        return url
    u = urllib.parse.urlparse(url)
    conn = HTTPConnection(u.hostname, u.port, timeout=60)
    conn.request("GET", u.path + "?" + u.query)
    r = conn.getresponse()
    if r.status in (301, 302, 303, 307, 308):
        loc = r.getheader("Location") or ""
        r.read()
        conn.close()
        if loc.startswith("magnet:"):
            return loc
        return resolve_grab(loc)
    body = r.read()
    conn.close()
    if body.startswith(b"d") or b"announce" in body[:512]:  # bencoded .torrent
        tmp = tempfile.NamedTemporaryFile(
            suffix=".torrent", delete=False)
        tmp.write(body)
        tmp.close()
        return tmp.name
    raise RuntimeError(f"cannot resolve grab URL (HTTP {r.status})")


def prowlarr(path, key):
    req = urllib.request.Request(PROWLARR + "/api/v1" + path,
                                 headers={"X-Api-Key": key})
    return json.load(urllib.request.urlopen(req, timeout=60))


# Streamability limits: the emby container is throttled to 40 Mbit
# (= 18 GB/hour), so anything fatter can never play back smoothly.
# Runtimes aren't known at search time, so these are conservative proxies.
EP_MAX = 12e9        # single episode  (~45-60 min)
MOVIE_MAX = 30e9     # movie           (~1.5-2.5 h)
PACK_MAX = 80e9      # season pack
REMUX = re.compile(r"remux", re.I)  # raw disc bitrate 40-80+ Mbit: never fits
# Dub markers (Spanish/Latino/Castilian, Russian MVO/DVO voice-overs, etc.)
DUB_PAT = re.compile(
    r"\b(dub(bed)?|lat(ino)?|cast(ellano)?|mvo|dvo|\byfx?\b)\b", re.I)
# Markers that the original audio is also present
ORIG_PAT = re.compile(
    r"\b(original|eng(lish)?|multi[dti]?|dual|jpn|jap(anese)?|vostfr?)\b", re.I)


def dub_only(title):
    """True if the release looks dub-only (no original audio track)."""
    return bool(DUB_PAT.search(title)) and not ORIG_PAT.search(title)


def size_limit(title):
    """Max acceptable release size in bytes for this title."""
    if re.search(r"[sS]\d{1,2}[eE]\d{1,3}", title):
        return EP_MAX
    if re.search(r"[sS]\d{1,2}\b|season\s*\d", title, re.I):
        return PACK_MAX
    return MOVIE_MAX


def over_limit(r):
    title = r.get("title") or ""
    if REMUX.search(title):
        return "remux"
    if (r.get("size") or 0) > size_limit(title):
        return "size"
    if dub_only(title):
        return "dub-only"
    return None


def score(r):
    title = (r.get("title") or "").lower()
    res = 0
    for w, v in (("2160p", 4), ("1080p", 3), ("720p", 2), ("480p", 1)):
        if w in title:
            res = v
            break
    s = res * 1000
    s += 200 if re.search(r"blu-?ray", title) else 0
    s += 100 if re.search(r"\bhdr\b|\bdv\b|dolby ?vision", title) else 0
    s += min(int(r.get("seeders") or 0), 500)       # seeders, capped
    if DUB_PAT.search(title):                       # dual/dub+orig is usable
        s -= 300                                    # but plain releases win
    size = r.get("size") or 0
    if size < 200e6:                                # junk
        s -= 100000
    return s


def prowlarr_key():
    key = load_env().get("PROWLARR_API_KEY")
    if not key:
        cfg = Path(__file__).resolve().parent.parent / "prowlarr" / "config.xml"
        key = re.search(r"<ApiKey>([^<]+)", cfg.read_text()).group(1)
    return key


def search(query, tv=True, allow_fat=False, key=None):
    """Return (acceptable releases best-first, releases refused as over-cap)."""
    cats = TV_CATS if tv else MOVIE_CATS
    q = urllib.parse.urlencode(
        [("query", query), ("limit", 100)]
        + [("categories", c) for c in cats.split(",")])
    results = [r for r in prowlarr(f"/search?{q}", key or prowlarr_key())
               if r.get("magnetUrl") or r.get("downloadUrl")]
    fat = []
    if not allow_fat:
        fat = [r for r in results if over_limit(r)]
        results = [r for r in results if not over_limit(r)]
    results.sort(key=score, reverse=True)
    return results, fat


def queue(r):
    """Send one release to Torbox."""
    grab = resolve_grab(r.get("magnetUrl") or r.get("downloadUrl"))
    print(f"queuing: {r.get('title')} "
          f"({(r.get('size') or 0)/1e9:.1f} GB, {r.get('seeders')} seeds)")
    add = Path(__file__).with_name("torbox_add.py")
    subprocess.run([sys.executable, str(add), grab], check=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    if not args:
        sys.exit(__doc__)
    query = args[0]
    pick = None
    if "-n" in flags:
        pick = int(args[1]) if len(args) > 1 else None
    results, fat = search(query, tv="--tv" in flags,
                          allow_fat="--allow-fat" in flags)
    if "--allow-fat" not in flags:
        if fat:
            print(f"skipped {len(fat)} over-limit releases "
                  f"(remux / >{int(EP_MAX//1e9)} GB ep / >{int(MOVIE_MAX//1e9)} GB movie / "
                  f">{int(PACK_MAX//1e9)} GB pack; --allow-fat to include)", file=sys.stderr)
        if not results:
            sys.exit(f"nothing streamable within the 40 Mbit cap for {query!r}; "
                     f"re-run with --allow-fat to see everything")
    if not results:
        sys.exit(f"no results with magnets for {query!r}")
    if "--list" in flags or pick is None and "-n" in flags:
        for i, r in enumerate(results[:10], 1):
            mark = f" [over cap: {over_limit(r)}]" if over_limit(r) else ""
            print(f"{i:2d}. [{r.get('indexer')}] {r.get('seeders')} seeds, "
                  f"{(r.get('size') or 0)/1e9:.1f} GB | {r.get('title')}{mark}")
        if pick is None:
            return
    n = (pick or 1) - 1
    if n < 0 or n >= len(results):
        sys.exit(f"pick 1-{min(len(results), 10)}")
    r = results[n]
    if over_limit(r):
        sys.exit(f"refusing: {r.get('title')} exceeds the 40 Mbit streamability "
                 f"cap ({over_limit(r)}); pick another or use --allow-fat")
    queue(r)


if __name__ == "__main__":
    main()
