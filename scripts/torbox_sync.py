#!/usr/bin/env python3
"""Sync Torbox downloads (torrents/usenet/web) into a local .strm library for Emby.

Reads credentials from ../.env (TORBOX_API_KEY). Idempotent: creates/updates
.strm files under library/tv and library/movies, removes stale ones.
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LIB_TV = BASE / "library" / "tv"
LIB_MOVIES = BASE / "library" / "movies"
API = "https://api.torbox.app/v1/api"
# Guards against a partial API result being mistaken for a mass delete: refuse
# to prune more than this share of an already-populated library unprompted.
MAX_PRUNE_FRACTION = 0.25
MASS_PRUNE_FLOOR = 20

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv",
             ".flv", ".webm", ".mpg", ".mpeg", ".m2ts", ".vob", ".ogm"}
# Below this a "video" file is a broken/placeholder upload, not real content
MIN_VIDEO_SIZE = 3 * 1024 * 1024
# A batch's biggest non-TV file only counts as "the real movie" (as opposed
# to an unusually long bonus clip) above this size
MAIN_FILE_MIN_SIZE = 700 * 1024 * 1024
TV_PAT = re.compile(r"(?:[sS]\d{1,2}[eE]\d{1,3}|\b\d{1,2}x\d{1,3}\b|"
                    r"\b[eE][pP]?\d{1,3}\b|[sS]eason\s*\d+)", re.I)
BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# Leading "SS.EE - title" episode numbers (no SxxExx token) that Emby cannot
# parse, e.g. "01.02 - Trouble in Lumpy Space.mp4"
DOT_EP_PAT = re.compile(r"^(\d{1,2})\.(\d{1,3})(\s*-\s*|\s+)")
# SxxExx (also SxxEPxx, or 3-4 digit season/ep like S002E062) anywhere in a filename
EP_TOKEN = re.compile(r"[sS](\d{1,4})[eE][pP]?(\d{1,4})")
# 7x01 style
X_TOKEN = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
# Anime-style absolute numbering: "[Group] Show Name - 03 [1080p...]" or
# "Show Name - E03 v2 [1080p...]" (literal E prefix, optional version tag)
# A bare number needs two digits to be an episode rather than part of the
# title, but an explicit "E" prefix is unambiguous, so "- E1 v2" counts too.
ANIME_EP = re.compile(r"^(?:\[[^\]]*\]\s*)*(?P<show>.+?)\s*-\s*"
                      r"(?:[eE](?P<ep_pfx>\d{1,3})|(?P<ep>\d{2,3}))"
                      r"(?:\s*-\s*\d{2,3})?"
                      r"(?:\s*v\d+)?\s*(?:[\[\(]|\.[A-Za-z0-9]{2,4}$|$)")
# Dot-separated absolute numbering with no dash: "Show.Name.53.v2.1080p..."
ANIME_EP_DOT = re.compile(
    r"^(?P<show>.+?)\.(?P<ep>\d{2,3})(?:\.v\d+)?(?=\.(?:2160p|1080p|720p|480p|"
    r"bluray|brrip|bdrip|web-?dl|hdtv|x26[45]|h\.?26[45]|hevc)\b)", re.I)
ANIME_SEASON = re.compile(r"\b[sS](\d{1,2})\s*$")
# Season hint from the immediate parent folder ("Attack on Titan Season 2"),
# used when a batch has one absolute-numbered subfolder per season - the
# filename alone ("Show - 26.mkv") carries no season, so without this every
# season collides into "Season 01".
FOLDER_SEASON = re.compile(r"\bseason\s*(\d{1,2})\b", re.I)
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
# A run's span ("Jujutsu Kaisen (2020-2023)") is not a year Emby can parse: it
# reads the folder as the title "Jujutsu Kaisen (2020". Keep the first year.
YEAR_RANGE_END = re.compile(r"\(((?:19|20)\d{2})\s*[-–—]\s*(?:19|20)?\d{2,4}\)\s*$")
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
    "eojjeolsuga eobsda": "no other choice",
    "darwin jihen": "the darwin incident",
    "jojos bizarre adventure (2012)": "jojo's bizarre adventure",
    "jojo's bizarre adventure part 6 - stone ocean": "jojo's bizarre adventure",
    "jojo no kimyou na bouken part 4 diamond wa kudakenai": "jojo's bizarre adventure",
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
    "2021 08 25)evangelion 3 333 you can (not) redo":
        "Evangelion 3.0 - You Can (Not) Redo (2012)",
}
# Series whose releases circulate under more than one title (romaji vs the
# English broadcast name). Without this the same show lands in two folders and
# the per-episode quality dedupe never sees the duplicates as duplicates.
SHOW_ALIASES = {
    "yani neko": "Chainsmoker Cat",
    "dcs legends of tomorrow": "DC's Legends of Tomorrow",
    "legends of tomorrow": "DC's Legends of Tomorrow",
}
# "<Show> OVA" / "<Show> Specials" is not a series of its own — Emby's
# convention is season 0 of the parent show, which is also the only way the
# bonus episodes inherit the parent's artwork and metadata instead of showing
# up as an unidentifiable extra entry beside it.
SPECIALS_SUFFIX = re.compile(r"\s*[-–:]?\s*\b(?:ovas?|specials?)\b\s*$", re.I)
# Kodi/Emby special-features folder names: content nested under one of these
# is attached to the parent movie instead of showing as its own movie card
EXTRAS_DIR_NAMES = {"featurettes", "extras", "extra", "behind the scenes",
                    "deleted scenes", "interviews", "scenes", "shorts",
                    "trailers", "other", "specials"}
# A short preview clip bundled alongside the real file, not the movie itself
SAMPLE_PAT = re.compile(r"\bsample\b", re.I)


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
    name = YEAR_RANGE_END.sub(r"(\1)", name)
    return name or None


def tv_target(item_name, rel_parts):
    """Return (series_dir, season, episode, raw_fname, name_tail, renumber_key)
    for an episode file, or None if it is not recognizably a TV episode.

    raw_fname is set (and name_tail is None) when the original filename
    already carries an SxxExx token Emby can parse untouched; otherwise
    name_tail holds the post-"S00E00 - " remainder and the caller builds the
    final filename once `episode` is final. renumber_key is (show, season)
    when `episode` is an absolute count borrowed from a season-per-folder
    batch (see FOLDER_SEASON below) rather than a true in-season number, so
    the caller can renumber it to a 1-based per-season sequence once every
    file in the item has been classified.
    """
    fname = rel_parts[-1]
    parent = rel_parts[-2] if len(rel_parts) > 1 else ""
    # Some groups separate every token with underscores rather than spaces or
    # dots ("[Cleo]Shinsekai_yori_-_01_(...)"). Underscore is a word char, so
    # the \s and \b anchors below never fire on those names and the whole
    # series is misread as a pile of movies. Substituting 1:1 keeps the string
    # length identical, so every match offset stays valid against `fname`.
    probe = fname.replace("_", " ")
    season = episode = None
    prefix = ""
    season_from_folder = False
    for pat in (EP_TOKEN, X_TOKEN):
        m = pat.search(probe)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            prefix = probe[:m.start()]
            break
    raw = season is not None
    if season is None:
        m = DOT_EP_PAT.match(probe)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if season is None:
        m = ANIME_EP.match(probe)
        if m and not re.search(r"\b(vol|part|chapter)\.?$", m.group("show"), re.I):
            episode = int(m.group("ep_pfx") or m.group("ep"))
            prefix = m.group("show")
            s = ANIME_SEASON.search(prefix)
            if s:
                season = int(s.group(1))
            else:
                fs = FOLDER_SEASON.search(parent)
                season = int(fs.group(1)) if fs else 1
                season_from_folder = bool(fs)
    if season is None:
        m = ANIME_EP_DOT.match(probe)
        if m:
            episode = int(m.group("ep"))
            prefix = m.group("show")
            s = ANIME_SEASON.search(prefix)
            if s:
                season = int(s.group(1))
            else:
                fs = FOLDER_SEASON.search(parent)
                season = int(fs.group(1)) if fs else 1
                season_from_folder = bool(fs)
    if season is None:
        return None
    show = clean_show(prefix) or clean_show(item_name)
    if not show:
        return None
    m = SPECIALS_SUFFIX.search(show)
    if m and show[:m.start()].strip(STRIP_CHARS):
        show = show[:m.start()].strip(STRIP_CHARS)
        season = 0
        season_from_folder = False
    show = SHOW_ALIASES.get(show.lower(), show)
    renumber_key = (show, season) if season_from_folder else None
    if raw:
        return show, season, episode, fname, None, None
    stem, ext = os.path.splitext(fname)
    return show, season, episode, None, f"{emby_friendly_name(stem)}{ext}", renumber_key


def load_env():
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api_get(path, key, attempts=4):
    # Torbox's edge routinely drops a TLS handshake or answers 403/520 under
    # load. Retry before giving up: a bare failure here used to propagate as
    # "this source owns nothing", which the prune below reads as a mandate to
    # delete every .strm it backs.
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": "debrid-emby-stack/1.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.load(r)
            if not payload.get("success"):
                raise RuntimeError(
                    f"Torbox API error on {path}: {payload.get('error')}")
            return payload.get("data") or []
        except Exception as e:
            if attempt == attempts - 1:
                raise
            print(f"[retry] {path}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt * 5)


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
        files = item.get("files") or []
        # The torrent's own top-level folder can disagree with the item's
        # display "name" (an indexer-formatted title carries a "[Batch]
        # (Alt Title)" suffix the actual folder never had) - strip whatever
        # the files themselves share, not what the item claims, or a
        # mismatch leaves the raw wrapper folder in every file's path below
        # (harmless for episodes, which tv_target() rebuilds from scratch,
        # but it leaves bonus/extras files nested under a stray folder that
        # Emby reads as a phantom extra season).
        names = [f["name"] for f in files]
        top_level = None
        if names and all("/" in n for n in names):
            segs = {n.split("/", 1)[0] for n in names}
            top_level = segs.pop() if len(segs) == 1 else None
        vids = []
        for f in files:
            ext = os.path.splitext(f.get("short_name") or f["name"])[1].lower()
            if ext not in VIDEO_EXT:
                continue
            # path of file relative to the item root folder
            rel = f["name"]
            if top_level and rel.startswith(top_level + "/"):
                rel = rel[len(top_level) + 1:]
            rel_parts = [sanitize(p) for p in rel.split("/") if p]
            if not rel_parts:
                continue
            if any(p.lower() in ("sample", "samples", "proof")
                   for p in rel_parts) \
                    or SAMPLE_PAT.search(os.path.splitext(rel_parts[-1])[0]):
                continue
            if (f.get("size") or 0) < MIN_VIDEO_SIZE:
                # too small to be real content (a broken/placeholder upload,
                # not even a legitimate short bonus clip)
                continue
            vids.append((f, rel_parts))

        # Classify every file up front so the movie branch can see the whole
        # batch instead of deciding file-by-file: a season pack's leftover
        # bonus clips need to know a real episode already claimed this item.
        classified = [(f, rel_parts, tv_target(item_name, rel_parts))
                      for f, rel_parts in vids]

        # A season-per-folder batch with absolute numbering ("Show Season 2/
        # Show - 26.mkv") gets its season from the folder but its episode is
        # still the whole-series absolute count; reset it to a 1-based
        # in-season sequence now that every file's episode number is known.
        renumber_groups = {}
        for i, (_, _, tgt) in enumerate(classified):
            if tgt and tgt[5]:
                renumber_groups.setdefault(tgt[5], []).append(i)
        for idxs in renumber_groups.values():
            idxs.sort(key=lambda i: classified[i][2][2])
            for new_ep, i in enumerate(idxs, start=1):
                f, rel_parts, tgt = classified[i]
                show, season, _, raw_fname, name_tail, renumber_key = tgt
                classified[i] = (f, rel_parts,
                                 (show, season, new_ep, raw_fname, name_tail,
                                  renumber_key))

        any_tv = any(tgt or TV_PAT.search(f["name"]) for f, _, tgt in classified)

        non_tv = [(f, rel_parts) for f, rel_parts, tgt in classified
                  if not tgt and not TV_PAT.search(f["name"])]
        # A bonus clip can legitimately run to a few hundred MB (a lossless
        # anime clean-ED, a making-of documentary), but nothing observed in
        # this library's actual bonus content comes close to a feature's
        # size — so "feature-length" is a much more reliable "this is a real
        # movie" signal than the file's own name. Every file clearing that
        # bar counts, not just the single largest: a franchise batch (e.g.
        # four recap movies with no year in any of their names) bundles
        # several real movies of similar size, and picking only the biggest
        # dropped the rest as if they were bonus clips.
        release_ids = set()
        main_titles = {}
        for f, rel_parts in non_tv:
            stem = os.path.splitext(rel_parts[-1])[0]
            title = clean_show(stem)
            is_feature = (f.get("size") or 0) >= MAIN_FILE_MIN_SIZE
            if is_feature or (title and YEAR_PAREN_END.search(title)):
                release_ids.add(id(f))
                main_titles.setdefault((title or stem).lower(), title or stem)
        distinct_mains = list(main_titles.values())

        for f, rel_parts, tgt in classified:
            if tgt:
                show, season, episode, raw_fname, name_tail, _ = tgt
                fname = raw_fname if raw_fname is not None \
                    else f"S{season:02d}E{episode:02d} - {name_tail}"
                parts = [sanitize(show), f"Season {season:02d}", fname]
                is_tv = True
            else:
                is_tv = TV_PAT.search(f["name"]) is not None
                if is_tv:
                    show_dir = clean_show(item_name) or item_name
                    # A bonus clip with no episode number of its own (an
                    # NCOP/NCED, a "Finale" special) still sits inside a
                    # "<Show> Season N" folder in a season-per-folder batch;
                    # fold it into the same normalized Season NN the real
                    # episodes land in instead of keeping the raw folder
                    # name, which Emby reads as a phantom extra season.
                    season_dir = None
                    for p in rel_parts[:-1]:
                        fs = FOLDER_SEASON.search(p)
                        if fs:
                            season_dir = f"Season {int(fs.group(1)):02d}"
                            break
                    if season_dir:
                        extras_dirs = [p for p in rel_parts[:-1]
                                       if p.lower() in EXTRAS_DIR_NAMES]
                        parts = [sanitize(show_dir), season_dir,
                                *extras_dirs, rel_parts[-1]]
                    else:
                        parts = [sanitize(show_dir), *rel_parts]
                    parts[-1] = emby_friendly_name(parts[-1])
                else:
                    stem = os.path.splitext(rel_parts[-1])[0]
                    stem_title = clean_show(stem)
                    is_release = id(f) in release_ids
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
                    # Always flatten to movie_dir/file or movie_dir/Featurettes/
                    # file: any preserved release-subfolder name (a site
                    # prefix, a shared collection wrapper, an uploader tag)
                    # risks Emby reading it as the title instead of movie_dir,
                    # and it leaves bonus clips orphaned with no sibling movie
                    # file whenever a *different* release wins the dedup below.
                    rel_parts = [rel_parts[-1]] if is_release \
                        else ["Featurettes", rel_parts[-1]]
                    parts = [sanitize(movie_dir), *rel_parts]
            parts[-1] += ".strm"
            yield (is_tv, parts, strm_url(kind, key, item["id"], f["id"]))


def main():
    key = load_env()["TORBOX_API_KEY"]
    wanted = {}  # Path -> url
    skipped = 0
    failed = []
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
            failed.append(kind)

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

    # Same idea for movies: multiple cached releases of the same title show
    # up as separate movie cards in Emby unless only the best one survives.
    # Bonus clips (already routed under a Featurettes/ dir) are left alone.
    tl = len(LIB_MOVIES.parts)
    groups = {}
    for path in wanted:
        if path.parts[:tl] != LIB_MOVIES.parts or len(path.parts) <= tl + 1:
            continue
        rel = path.parts[tl + 1:]
        if any(p.lower() in EXTRAS_DIR_NAMES for p in rel[:-1]):
            continue
        groups.setdefault(path.parts[tl], []).append(path)
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

    # Pruning is only safe when `wanted` is a complete picture of the account.
    # A source that errored contributes nothing, so every .strm it backs would
    # look stale — one dropped TLS handshake once deleted 1137 files this way.
    stale = [p for root in (LIB_TV, LIB_MOVIES)
             for p in sorted(root.rglob("*.strm"), reverse=True)
             if p not in wanted]
    existing = sum(1 for root in (LIB_TV, LIB_MOVIES)
                   for _ in root.rglob("*.strm"))
    if failed:
        prune_block = f"{','.join(failed)} failed to list"
    elif (len(stale) > existing * MAX_PRUNE_FRACTION
            and existing >= MASS_PRUNE_FLOOR
            and "--allow-mass-prune" not in sys.argv):
        # Nothing raised, but a truncated 200 looks identical to a real mass
        # delete. Make the caller confirm rather than guessing which it was.
        prune_block = (f"would delete {len(stale)}/{existing} files; "
                       f"re-run with --allow-mass-prune if intended")
    else:
        prune_block = None

    removed = 0
    if prune_block:
        print(f"[skip-prune] {prune_block}", file=sys.stderr)
    else:
        for p in stale:
            p.unlink()
            removed += 1
        # prune empty dirs bottom-up
        for root in (LIB_TV, LIB_MOVIES):
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
