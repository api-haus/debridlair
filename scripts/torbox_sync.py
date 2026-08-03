#!/usr/bin/env python3
"""Sync Torbox downloads (torrents/usenet/web) into a local .strm library for Emby.

Reads credentials from ../.env (TORBOX_API_KEY). Idempotent: creates/updates
.strm files under library/tv and library/movies, removes stale ones.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LIB_TV = BASE / "library" / "tv"
LIB_MOVIES = BASE / "library" / "movies"
API = "https://api.torbox.app/v1/api"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv",
             ".flv", ".webm", ".mpg", ".mpeg", ".m2ts", ".vob", ".ogm"}
TV_PAT = re.compile(r"(?:[sS]\d{1,2}[eE]\d{1,3}|\b\d{1,2}x\d{1,3}\b|"
                    r"[eE][pP]?\d{1,3}\b|[sS]eason\s*\d+)", re.I)
BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# Leading "SS.EE - title" episode numbers (no SxxExx token) that Emby cannot
# parse, e.g. "01.02 - Trouble in Lumpy Space.mp4"
DOT_EP_PAT = re.compile(r"^(\d{1,2})\.(\d{1,3})(\s*-\s*|\s+)")
# SxxExx (also SxxEPxx, or 3-4 digit season/ep like S002E062) anywhere in a filename
EP_TOKEN = re.compile(r"[sS](\d{1,4})[eE][pP]?(\d{1,4})")
# 7x01 style
X_TOKEN = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
# Anime-style absolute numbering: "[Group] Show Name - 03 [1080p...]"
ANIME_EP = re.compile(r"^(?:\[[^\]]*\]\s*)*(?P<show>.+?)\s*-\s*"
                      r"(?P<ep>\d{2,3})(?:\s*-\s*\d{2,3})?\s*(?:[\[\(]|$)")
# Dot-separated absolute numbering with no dash: "Show.Name.53.v2.1080p..."
ANIME_EP_DOT = re.compile(
    r"^(?P<show>.+?)\.(?P<ep>\d{2,3})(?:\.v\d+)?(?=\.(?:2160p|1080p|720p|480p|"
    r"bluray|brrip|bdrip|web-?dl|hdtv|x26[45]|h\.?26[45]|hevc)\b)", re.I)
ANIME_SEASON = re.compile(r"\b[sS](\d{1,2})\s*$")
# Release-name junk: leading site/group prefixes
SITE_PFX = re.compile(r"^(?:\[[^\]]*\]|www\.?\S+|[\w-]+\.(?:org|com|net|to|io|me|tv|bz|mx)(?:\s*[-–—]+\s*|\s{2,}))", re.I)
# Cut a show/movie name at the first quality/release tag
QUALITY_CUT = re.compile(
    r"\b(2160p|1080p|720p|480p|576p|4k|8k|uhd|blu-?ray|bdrip|brrip|remux|"
    r"web-?dl|webmux|webrip|web|hdtv|hdrip|dvdrip|dvd|x26[45]|h\.?26[45]|"
    r"hevc|avc|av1|xvid|aac2?|ac-?3|eac3|ddp?|dd\+|atmos|truehd|opus|"
    r"dts(-?hd)?|flac|hdr10\+?|hdr|dolby|vision|dv|sdr|repack|proper|"
    r"internal|limited|complete|series|seasons?|imax|extended|remaster(?:ed)?|"
    r"hybrid|final|custom|upscale[dr]?|multi[dti]?|multi-?audio|multi-?subs?|"
    r"subbed|subs?|uncensored|batch|pack|\d+\s?bits?|[257]\.[01]|"
    r"ita|eng|jpn|jap|kor|hun|esp|lat|fre|ger|rus|ukr|dual-?audio|"
    r"mp4|mkv|avi|www\.\S+)\b", re.I)
# Cut at season/episode tokens: S01, S01E02, S01-S02, Season 1, Season 01 and 02
SHOW_TOKEN_CUT = re.compile(
    r"\b(?:s\d{1,2}(?:e\d{1,3})?(?:\s*-\s*s?\d{1,2}(?:e\d{1,3})?)?|"
    r"season\s*\d+(?:\s*(?:and|&)\s*\d+)*)\b", re.I)
YEAR_END = re.compile(r"\b((?:19|20)\d{2})\s*$")
STRIP_CHARS = " -–—,;(["
# Bonus material that should stay inside the parent movie's folder
EXTRAS_PAT = re.compile(
    r"\b(trailer|teaser|featurette|behind|deleted|gag|interview|bts|making|"
    r"bonus|extras?|promo|shorts?|scenes?)\b", re.I)
# Anime clean opening/ending songs: "NCOP1v2", "NCED2" (glued to digits, so
# EXTRAS_PAT's trailing \b would never match)
NC_TAG_PAT = re.compile(r"\bnc(?:op|ed)", re.I)
# A clean_show() result that looks like an actual release ("Title (2021)"),
# as opposed to a bonus clip or collection wrapper with no title of its own
YEAR_PAREN_END = re.compile(r"\((?:19|20)\d{2}\)$")
# Known duplicate-name variants for the same title, keyed by lowercased
# variant name -> lowercased canonical name (release names disagree on
# subtitle/spelling; not safe to derive this generically)
ALIASES = {
    "star wars andor": "andor",
    "super natural": "supernatural",
    "no hay otra opcion (no other choice)": "no other choice",
}
# One-off clean_show() misfires that aren't worth a general regex fix:
# dot-decimal version numbers ("1.11") collapse into spaces, and one release
# group mislabeled its year. Keyed by lowercased computed movie_dir.
TITLE_OVERRIDES = {
    "evangelion 1 11 - you are (not) alone":
        "Evangelion 1.11 - You Are (Not) Alone (2007)",
    "evangelion 2 22 - you can (not) advance":
        "Evangelion 2.22 - You Can (Not) Advance (2009)",
    "evangelion 3 0+1 11 - thrice upon a time (bd":
        "Evangelion 3.0+1.0 - Thrice Upon a Time (2021)",
    "neon genesis evangelion - the end of evangelion (1995)":
        "Neon Genesis Evangelion - The End of Evangelion (1997)",
    "gojira 1954 rm4k": "Godzilla (1954)",
}


def emby_friendly_name(filename):
    """Rewrite a filename so Emby can parse season/episode from it."""
    m = DOT_EP_PAT.match(filename)
    if m:
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d} - " + \
            filename[m.end():]
    return filename


def clean_show(name):
    """Extract a clean show/movie name from a release name, or None."""
    name = urllib.parse.unquote(name)
    for _ in range(3):  # strip stacked prefixes: "[Group] www.site.org - Show"
        stripped = SITE_PFX.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    name = re.sub(r"\[[^\]]*\]\s*$", "", name)  # trailing "[ UIndex.org ]"
    m = SHOW_TOKEN_CUT.search(name)
    if m:
        name = name[:m.start()]
    m = QUALITY_CUT.search(name)
    if m:
        name = name[:m.start()]
    name = re.sub(r"[._]+", " ", name).strip(STRIP_CHARS)
    name = YEAR_END.sub(r"(\1)", name)
    return name or None


def tv_target(item_name, fname):
    """Return (series_dir, season_dir, filename) for an episode file, or None
    if it is not recognizably a TV episode."""
    season = episode = None
    prefix = ""
    for pat in (EP_TOKEN, X_TOKEN):
        m = pat.search(fname)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            prefix = fname[:m.start()]
            break
    if season is None:
        m = DOT_EP_PAT.match(fname)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if season is None:
        m = ANIME_EP.match(fname)
        if m and not re.search(r"\b(vol|part|chapter)\.?$", m.group("show"), re.I):
            episode = int(m.group("ep"))
            prefix = m.group("show")
            s = ANIME_SEASON.search(prefix)
            season = int(s.group(1)) if s else 1
    if season is None:
        m = ANIME_EP_DOT.match(fname)
        if m:
            episode = int(m.group("ep"))
            prefix = m.group("show")
            s = ANIME_SEASON.search(prefix)
            season = int(s.group(1)) if s else 1
    if season is None:
        return None
    show = clean_show(prefix) or clean_show(item_name)
    if not show:
        return None
    if not EP_TOKEN.search(fname):
        stem, ext = os.path.splitext(fname)
        fname = f"S{season:02d}E{episode:02d} - {emby_friendly_name(stem)}{ext}"
    return show, f"Season {season:02d}", fname


def load_env():
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api_get(path, key):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": "debrid-emby-stack/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    if not payload.get("success"):
        raise RuntimeError(f"Torbox API error on {path}: {payload.get('error')}")
    return payload.get("data") or []


def sanitize(part):
    part = BAD_CHARS.sub("", part).strip().strip(".")
    return part or "untitled"


def strm_url(kind, key, item_id, file_id):
    param = {"torrents": "torrent_id", "usenet": "usenet_id",
             "webdl": "web_id"}[kind]
    q = urllib.parse.urlencode({"token": key, param: item_id,
                                "file_id": file_id, "redirect": "true"})
    return f"{API}/{kind}/requestdl?{q}"


def collect(kind, key):
    """Yield (relpath_in_library, strm_url) for every video file of this kind."""
    items = api_get(f"/{kind}/mylist?bypass_cache=true", key)
    for item in items:
        state = (item.get("download_state") or "").lower()
        if state not in ("cached", "completed", "downloaded", "seeding", "uploading") \
                and not item.get("download_present"):
            continue
        item_name = sanitize(item.get("name") or f"{kind}-{item['id']}")
        vids = []
        for f in item.get("files") or []:
            ext = os.path.splitext(f.get("short_name") or f["name"])[1].lower()
            if ext not in VIDEO_EXT:
                continue
            # path of file relative to the item root folder
            rel = f["name"]
            prefix = (item.get("name") or "") + "/"
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
            rel_parts = [sanitize(p) for p in rel.split("/") if p]
            if not rel_parts:
                continue
            if any(p.lower() in ("sample", "samples", "proof")
                   for p in rel_parts) \
                    or rel_parts[-1].lower().startswith("sample"):
                continue
            vids.append((f, rel_parts))

        # Classify every file up front so the movie branch can see the whole
        # batch instead of deciding file-by-file: a season pack's leftover
        # bonus clips need to know a real episode already claimed this item.
        classified = [(f, rel_parts, tv_target(item_name, rel_parts[-1]))
                      for f, rel_parts in vids]
        any_tv = any(tgt or TV_PAT.search(f["name"]) for f, _, tgt in classified)

        non_tv = [(f, rel_parts) for f, rel_parts, tgt in classified
                  if not tgt and not TV_PAT.search(f["name"])]
        main_titles = {}
        for f, rel_parts in non_tv:
            stem = os.path.splitext(rel_parts[-1])[0]
            title = clean_show(stem)
            if title and YEAR_PAREN_END.search(title):
                main_titles.setdefault(title.lower(), title)
        distinct_mains = list(main_titles.values())

        for f, rel_parts, tgt in classified:
            if tgt:
                parts = [sanitize(tgt[0]), tgt[1], tgt[2]]
                is_tv = True
            else:
                is_tv = TV_PAT.search(f["name"]) is not None
                if is_tv:
                    show_dir = clean_show(item_name) or item_name
                    parts = [sanitize(show_dir), *rel_parts]
                    parts[-1] = emby_friendly_name(parts[-1])
                else:
                    stem = os.path.splitext(rel_parts[-1])[0]
                    stem_title = clean_show(stem)
                    is_release = bool(stem_title and YEAR_PAREN_END.search(stem_title))
                    if any_tv and not is_release:
                        # TV bonus material (NCOP/NCED, "making of", featurette)
                        # with no episode of its own and no home in movies/
                        continue
                    if len(distinct_mains) > 1:
                        # a real multi-movie collection: each file is its own movie
                        movie_dir = stem_title or clean_show(item_name) or item_name
                    elif any_tv:
                        # one real movie bundled inside a season+movie batch
                        movie_dir = stem_title or clean_show(item_name) or item_name
                    elif len(vids) > 1 and (len(rel_parts) > 1
                                            or EXTRAS_PAT.search(stem)
                                            or NC_TAG_PAT.search(stem)):
                        # bonus material of a single movie: keep it with the
                        # main movie instead of splitting it out
                        movie_dir = clean_show(item_name) or item_name
                    else:
                        movie_dir = stem_title or clean_show(item_name) or item_name
                    if movie_dir and not YEAR_PAREN_END.search(movie_dir):
                        # a bare release-group tag (e.g. a torrent whose only
                        # video file is literally named "ETRG.mp4") shouldn't
                        # beat a properly-descriptive torrent name
                        alt = clean_show(item_name)
                        if alt and YEAR_PAREN_END.search(alt):
                            movie_dir = alt
                    movie_dir = TITLE_OVERRIDES.get(
                        (movie_dir or "").lower(), movie_dir)
                    parts = [sanitize(movie_dir), *rel_parts]
            parts[-1] += ".strm"
            yield (is_tv, parts, strm_url(kind, key, item["id"], f["id"]))


def main():
    key = load_env()["TORBOX_API_KEY"]
    wanted = {}  # Path -> url
    skipped = 0
    for kind in ("torrents", "usenet", "webdl"):
        try:
            for is_tv, parts, url in collect(kind, key):
                root = LIB_TV if is_tv else LIB_MOVIES
                path = root.joinpath(*parts)
                if path in wanted and wanted[path] != url:
                    skipped += 1  # duplicate source for the same file; first wins
                    continue
                wanted[path] = url
        except Exception as e:
            print(f"[warn] {kind}: {e}", file=sys.stderr)

    # Merge top-level dirs that differ only by case or a "(year)" suffix
    # ("rick and morty" / "Rick and Morty", "Fallout" / "Fallout (2024)").
    for root in (LIB_TV, LIB_MOVIES):
        tl = len(root.parts)
        counts = {}
        for path in wanted:
            if path.parts[:tl] == root.parts and len(path.parts) > tl + 1:
                top = path.parts[tl]
                counts[top] = counts.get(top, 0) + 1
        groups = {}
        alias_bases = set()
        for top in counts:
            stripped = re.sub(r"\s*\((?:19|20)\d{2}(?:-(?:19|20)\d{2})?\)$",
                              "", top).lower()
            aliased = ALIASES.get(top.lower()) or ALIASES.get(stripped)
            base = aliased or stripped
            if aliased:
                alias_bases.add(base)
            groups.setdefault(base, []).append(top)
        canonical = {}
        for base, variants in groups.items():
            if len(variants) < 2:
                continue
            yeared = [v for v in variants
                      if re.search(r"\((?:19|20)\d{2}(?:-(?:19|20)\d{2})?\)$", v)]
            if base in alias_bases:
                # a known alias: prefer the variant matching the canonical
                # spelling verbatim, and a non-shouty casing of it
                exact = sorted((v for v in variants if v.lower() == base),
                              key=lambda v: v.isupper())
                canon = exact[0] if exact else \
                    (yeared[0] if yeared else max(variants, key=counts.get))
            else:
                canon = yeared[0] if yeared else max(variants, key=counts.get)
            for v in variants:
                if v != canon:
                    canonical[v] = canon
        if not canonical:
            continue
        merged = {}
        for path, url in wanted.items():
            if path.parts[:tl] == root.parts and len(path.parts) > tl + 1:
                top = path.parts[tl]
                if top in canonical:
                    path = root.joinpath(canonical[top],
                                         *path.parts[tl + 1:])
            if path in merged and merged[path] != url:
                skipped += 1
                continue
            merged[path] = url
        wanted = merged

    # Episodes present in multiple releases: keep only the best copy, or Emby
    # lists the episode twice. Score by resolution, then remux/blu-ray, then HDR.
    def qscore(fname):
        f = fname.lower()
        if "remux" in f:  # disc bitrate 40-80+ Mbit: unstreamable over the cap
            return -1000
        if re.search(r"\bai[\s._-]?upscale[dr]?\b", f):  # fake resolution
            return -2000
        res = 0
        for w, v in (("2160p", 4), ("1080p", 3), ("720p", 2), ("480p", 1)):
            if w in f:
                res = v
                break
        return res * 100 + (10 if re.search(r"blu-?ray", f) else 0) \
            + (5 if re.search(r"\bhdr\b|\bdv\b|dolby ?vision", f) else 0) \
            - (30 if re.search(r"\b(ita|hun|esp|lat|ger|fre|rus|ukr|mvo|dvo)\b", f)
               and not re.search(r"\b(eng|multi|dual)\b", f) else 0)

    tl = len(LIB_TV.parts)
    groups = {}
    for path in wanted:
        if path.parts[:tl] != LIB_TV.parts or len(path.parts) != tl + 3:
            continue
        m = EP_TOKEN.search(path.parts[-1])
        sm = re.fullmatch(r"Season (\d+)", path.parts[tl + 1])
        if not m or not sm:
            continue
        key = (path.parts[tl].lower(), int(sm.group(1)), int(m.group(2)))
        groups.setdefault(key, []).append(path)
    deduped = 0
    for entries in groups.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda p: qscore(p.name), reverse=True)
        for loser in entries[1:]:
            del wanted[loser]
            deduped += 1

    created = updated = 0
    for path, url in wanted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            created += 1
            path.write_text(url)
        elif path.read_text() != url:
            updated += 1
            path.write_text(url)

    removed = 0
    for root in (LIB_TV, LIB_MOVIES):
        for p in sorted(root.rglob("*.strm"), reverse=True):
            if p not in wanted:
                p.unlink()
                removed += 1
        # prune empty dirs bottom-up
        for d in sorted((d for d in root.rglob("*") if d.is_dir()),
                        key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    print(f"strm sync: {len(wanted)} wanted, {created} created, "
          f"{updated} updated, {removed} removed, {skipped} dup-skipped, "
          f"{deduped} lower-quality dupes dropped")


if __name__ == "__main__":
    main()
