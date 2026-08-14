#!/usr/bin/env python3
"""Sync Torbox downloads (torrents/usenet/web) into a local .strm library for Emby.

Reads credentials from ../.env (TORBOX_API_KEY). Idempotent: creates/updates
.strm files under library/tv and library/movies, removes stale ones.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cue  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
LIB_TV = BASE / "library" / "tv"
LIB_MOVIES = BASE / "library" / "movies"
LIB_MUSIC = BASE / "library" / "music"
LIB_ROOTS = (LIB_TV, LIB_MOVIES, LIB_MUSIC)
API = "https://api.torbox.app/v1/api"
# The cueslice service cuts one track out of a single-file rip (docs/library.md)
CUESLICE = os.environ.get("CUESLICE_URL", "http://cueslice:8099")
CUE_CACHE = BASE / "sync-state" / "cue_sheets.json"
MAX_CUE_SIZE = 512 * 1024
# Guards against a partial API result being mistaken for a mass delete: refuse
# to prune more than this share of an already-populated library unprompted.
MAX_PRUNE_FRACTION = 0.25
MASS_PRUNE_FLOOR = 20

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv",
             ".flv", ".webm", ".mpg", ".mpeg", ".m2ts", ".ogm"}
# .vob deliberately excluded: a raw DVD title fragment (VIDEO_TS/VTS_NN_M.VOB)
# has no filename that says which fragment is the feature vs. a menu loop,
# unlike a demuxed release — so it's never synced in as a library item.
AUDIO_EXT = {".flac", ".ape", ".wv", ".alac", ".m4a", ".mp3", ".ogg", ".opus",
             ".wav", ".aiff", ".aif", ".dsf", ".dff", ".mpc", ".tta", ".wma"}
LOSSLESS_EXT = {".flac", ".ape", ".wv", ".alac", ".wav", ".aiff", ".aif",
                ".dsf", ".dff", ".tta"}
# Below this a "video" file is a broken/placeholder upload, not real content
MIN_VIDEO_SIZE = 3 * 1024 * 1024
# Below this an "audio" file is a rip artefact, not a piece of music
MIN_AUDIO_SIZE = 200 * 1024
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
# Anime-style absolute numbering, "[Group] Show - 03 [1080p]" (docs/library.md)
ANIME_EP = re.compile(r"^(?:\[[^\]]*\]\s*)*(?P<show>.+?)\s*-\s*"
                      r"(?:[eE](?P<ep_pfx>\d{1,3})|(?P<ep>\d{2,3}))"
                      r"(?:\s*-\s*\d{2,3})?"
                      r"(?:\s*v\d+)?\s*(?:[\[\(]|\.[A-Za-z0-9]{2,4}$|$)")
# Dot-separated absolute numbering with no dash: "Show.Name.53.v2.1080p..."
ANIME_EP_DOT = re.compile(
    r"^(?P<show>.+?)\.(?P<ep>\d{2,3})(?:\.v\d+)?(?=\.(?:2160p|1080p|720p|480p|"
    r"bluray|brrip|bdrip|web-?dl|hdtv|x26[45]|h\.?26[45]|hevc)\b)", re.I)
ANIME_SEASON = re.compile(r"\b[sS](\d{1,2})\s*$")
# Season hint from the parent folder, for absolute-numbered batches that carry
# no season in the filename at all (docs/library.md)
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
    "ki-duk kim - bi-mong aka dream (2008)": "Dream (2008)",
    "elsecretodeunaobsesionm1080": "Secret in Their Eyes (2015)",
    "arirang 2011 dvdrip [1 46]": "Arirang (2011)",
}
# Series whose releases circulate under more than one title (romaji vs the
# English broadcast name). Without this the same show lands in two folders and
# the per-episode quality dedupe never sees the duplicates as duplicates.
SHOW_ALIASES = {
    "yani neko": "Chainsmoker Cat",
    "dcs legends of tomorrow": "DC's Legends of Tomorrow",
    "legends of tomorrow": "DC's Legends of Tomorrow",
}
# "<Show> OVA"/"<Show> Specials" is season 0 of the parent (docs/library.md)
SPECIALS_SUFFIX = re.compile(r"\s*[-–:]?\s*\b(?:ovas?|specials?)\b\s*$", re.I)
# Kodi/Emby special-features folder names: content nested under one of these
# is attached to the parent movie instead of showing as its own movie card
EXTRAS_DIR_NAMES = {"featurettes", "extras", "extra", "behind the scenes",
                    "deleted scenes", "interviews", "scenes", "shorts",
                    "trailers", "other", "specials"}
# A short preview clip bundled alongside the real file, not the movie itself
SAMPLE_PAT = re.compile(r"\bsample\b", re.I)
# Bracketed format/rip junk trailing a music release name (docs/library.md)
AUDIO_TAG_BRACKET = re.compile(
    r"\s*[\[\(\{][^\]\)\}]*\b(?:flac|ape|wv|wavpack|alac|mp3|aac|ogg|opus|wav|"
    r"dsd|sacd|lossless|lossy|\d{1,2}\s?bits?|\d{2,3}(?:[.,]\d)?\s?khz|"
    r"\d{3}\s?kbps|eac|cue|log|scans?|vbr|cbr|vinyl|web|cd|reissue|"
    r"remaster(?:ed)?|mono|stereo)\b[^\]\)\}]*[\]\)\}]", re.I)
# The same junk unbracketed, at the end: "..._Scans", "... - 24bit 96kHz"
AUDIO_TAG_TAIL = re.compile(
    r"[\s_.,-]*\b(?:flac|ape|wv|wavpack|alac|mp3|aac|lossless|scans?|eac|cue|"
    r"log|vbr|cbr|\d{1,2}\s?bits?|\d{2,3}(?:[.,]\d)?\s?khz|\d{3}\s?kbps|"
    r"reissue|remaster(?:ed)?|web|vinyl)\b[\s_.,-]*$", re.I)
YEAR_ANY = re.compile(r"\b((?:19|20)\d{2})\b")
# A discography's span, "(1988-2012)": one year, not two, and not a title
YEAR_SPAN = re.compile(r"\(?\b((?:19|20)\d{2})\s*[-–—]\s*(?:19|20)?\d{2,4}\)?")
BARE_YEAR = re.compile(r"^\(?((?:19|20)\d{2})\)?$")
REISSUE_YEAR = re.compile(r"[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]?")
TRAILING_YEAR = re.compile(r"[\s,;.\-–—]*\b(?:19|20)\d{2}\s*$")
# A concert's full date, "1999.12.03": the year is the year, the rest is not
DATE_STAMP = re.compile(r"\b((?:19|20)\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b")
# "CD1", "Disc 2", "CD.03" — a disc level Emby keeps inside the album
DISC_DIR = re.compile(r"^(?:cd|disc|disk)[\s._-]*(\d{1,2})$", re.I)
# Artwork and rip-log folders that carry no audio worth filing on its own
MUSIC_JUNK_DIR = {"scans", "scan", "artwork", "art", "covers", "cover",
                  "booklet", "logs", "log", "info"}
# A dash with whitespace on one side: splits "Artist - Album", not "Post-Bop"
DASH_SPLIT = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")
# Everything from the first format token on is rip notes, not a title. A
# bracket cannot be trusted to close them: "Monism , FLAC (tracks" is real.
AUDIO_CUT = re.compile(
    r"[\s,;(\[{]+(?:flac|ape|wv|wavpack|alac|mp3|aac|ogg|opus|wav|dsd|dsf|"
    r"sacd|lossless|lossy|tracks?\s*\+|image\s*\+|image\b|eac|cue|scans?|"
    r"vbr|cbr|\d{3}\s?kbps|\d{1,2}\s?bits?|\d{2,3}(?:[.,]\d)?\s?khz|"
    r"web-?dl|satrip|dvdrip|bdrip|hdtv|x26[45])\b", re.I)
# "2 x CD", "3CD": how the release was pressed, not what it is called
DISC_COUNT = re.compile(r"[,;]?\s*\b\d{1,2}\s*[x×]?\s*cds?\b\s*", re.I)
EMPTY_PARENS = re.compile(r"[\(\[\{]\s*[\)\]\}]")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# An album folder's cover, best name first; anything else falls back to size
COVER_NAMES = ("front", "cover", "folder", "booklet front", "f1", "album")
BACK_COVER = re.compile(r"\b(back|tray|inlay|inside|disc|cd\d?|obi|spine)\b",
                        re.I)
# A cover this big is a full-resolution scan, not artwork Emby should hold
MAX_COVER_SIZE = 20 * 1024 * 1024


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
    # Underscore is a word char, so \s and \b never fire on underscore-separated
    # names; the 1:1 swap keeps every match offset valid (docs/library.md)
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


def clean_audio_tags(name):
    """Strip format, bitrate and rip-log junk from a music release name."""
    # A leading "(Free Jazz, Avant-Garde)" is the tracker's genre list and a
    # "{Blue Note}" anywhere is the label; neither belongs in a folder name
    name = re.sub(r"^\s*\([^)]*\)\s*", "", name)
    # "{Blue Note}" and a trailing "[DIW Records DIW-916]" are the label and
    # its catalogue number, which no album is called
    name = re.sub(r"\s*\{[^}]*\}\s*", " ", name)
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name)
    for _ in range(4):
        stripped = AUDIO_TAG_TAIL.sub(
            "", AUDIO_TAG_BRACKET.sub(" ", name)).strip(STRIP_CHARS + ")]}")
        if stripped == name:
            break
        name = stripped
    m = AUDIO_CUT.search(name)
    if m and name[:m.start()].strip(STRIP_CHARS):
        name = name[:m.start()]
    name = DISC_COUNT.sub(" ", name)
    name = re.sub(r"\s{2,}", " ", EMPTY_PARENS.sub(" ", name)).strip(
        STRIP_CHARS)
    # Stripping junk off the end can eat a closing bracket the title needed
    return name + ")" * (name.count("(") - name.count(")"))


def split_artist_album(name, default_artist=None):
    """Split a music release name into (artist, album, year); any may be None."""
    name = urllib.parse.unquote(name)
    for _ in range(3):
        stripped = SITE_PFX.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    name = re.sub(r"[_]+", " ", name)
    stamp = DATE_STAMP.search(name)
    if stamp:
        name = name[:stamp.start()] + stamp.group(1) + name[stamp.end():]
    name = clean_audio_tags(YEAR_SPAN.sub(r"\1", name))
    # "1996-Godspelized" glues the year on with no space, so the dash split
    # below never sees it and the year ends up inside the album name
    year_pfx = re.match(r"^\(?((?:19|20)\d{2})\)?[\s.\-–—]+(?=\S)", name)
    if year_pfx:
        name = name[year_pfx.end():]
    segs = [s.strip(STRIP_CHARS) for s in DASH_SPLIT.split(name)]
    segs = [s for s in segs if s]
    if not segs:
        return default_artist, None, None
    artist = default_artist
    if len(segs) > 1 and not BARE_YEAR.match(segs[0]):
        artist = segs.pop(0)
        # A trailing parenthetical on the artist is a sideman list, not part
        # of the name — without this every quartet becomes its own artist
        artist = re.sub(r"\s*\([^)]*\)\s*$", "", artist).strip() or artist
        # "David S. Ware, Cooper-Moore, William Parker" is a credit list, and
        # filing each line-up separately scatters one artist's catalogue. A
        # one-word head is a band ("Earth, Wind & Fire") and is left alone.
        head = artist.split(",")[0].strip()
        if "," in artist and len(head.split()) > 1:
            artist = head
    year = int(year_pfx.group(1)) if year_pfx else None
    if year is None and segs and BARE_YEAR.match(segs[0]):
        year = int(BARE_YEAR.match(segs.pop(0)).group(1))
    album = " - ".join(segs).strip(STRIP_CHARS) or None
    if album and year is None:
        m = YEAR_ANY.search(album)
        if m and album[:m.start()].strip(STRIP_CHARS):
            year = int(m.group(1))
            album = (album[:m.start()] + " " + album[m.end():]).strip(
                STRIP_CHARS)
    if album:
        # A second year is the reissue date; the recording date already won
        album = REISSUE_YEAR.sub(" ", album)
        if year is not None:
            album = TRAILING_YEAR.sub("", album)
        album = clean_audio_tags(album) or None
    return artist or default_artist, album, year


def album_dir(album, year):
    return f"{album} ({year})" if album and year else album


def music_target(item_name, rel_parts, sheet=None):
    """Return (artist_dir, album_dir, disc_dirs) for one audio file, or None."""
    item_artist, item_album, item_year = split_artist_album(item_name)
    if sheet:
        # A sheet states the artist and album outright, which a torrent name
        # only implies — and for a single-file rip it is the one place they are
        item_artist = sheet.get("performer") or item_artist
        item_album = sheet.get("title") or item_album
        item_year = sheet.get("date") or item_year
    dirs = [p for p in rel_parts[:-1] if p.lower() not in MUSIC_JUNK_DIR]
    discs = [p for p in dirs if DISC_DIR.match(p)]
    named = [p for p in dirs if not DISC_DIR.match(p)]
    artist, album, year = item_artist, item_album, item_year
    if named:
        # A discography box: the deepest named folder is the album, and the
        # item name is only good for the artist
        sub_artist, sub_album, sub_year = split_artist_album(
            named[-1], default_artist=item_artist)
        if sub_album:
            artist, album, year = sub_artist, sub_album, sub_year
    if not artist and not album:
        return None
    if not album:
        album = item_name
    if not artist:
        # No "Artist - Album" split in the name; the probe's tags will fix it
        artist = album
    disc_dirs = [f"CD {int(DISC_DIR.match(d).group(1)):02d}" for d in discs]
    return artist, album_dir(album, year), disc_dirs


def load_cue(kind, key, item_id, cue_file):
    """Fetch and parse one CUE sheet, remembering it across runs."""
    ident = f"{kind}:{item_id}:{cue_file['id']}"
    try:
        cache = json.loads(CUE_CACHE.read_text())
    except Exception:
        cache = {}
    if ident in cache:
        return cache[ident]
    if (cue_file.get("size") or 0) > MAX_CUE_SIZE:
        return None
    try:
        req = urllib.request.Request(
            strm_url(kind, key, item_id, cue_file["id"]),
            headers={"User-Agent": "debrid-emby-stack/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            sheet = cue.parse(cue.decode(r.read(MAX_CUE_SIZE)))
    except Exception as e:
        print(f"[warn] cue {cue_file['name']}: {e}", file=sys.stderr)
        return None
    cache[ident] = sheet
    try:
        CUE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CUE_CACHE.write_text(json.dumps(cache))
    except Exception as e:
        print(f"[warn] cannot cache cue: {e}", file=sys.stderr)
    return sheet


def match_sheets(kind, key, item_id, files, auds):
    """Map an audio file's id to the CUE sheet that cuts it into tracks."""
    sheets = {}
    cues = [f for f in files
            if os.path.splitext(f.get("short_name") or f["name"])[1].lower()
            == ".cue"]
    if not cues:
        return sheets
    parsed = [(c, load_cue(kind, key, item_id, c)) for c in cues]
    parsed = [(c, s) for c, s in parsed if s and cue.is_image(s)]
    for f, rel_parts in auds:
        stem = os.path.splitext(rel_parts[-1])[0].lower()
        for c, sheet in parsed:
            named = os.path.splitext(sheet["files"][0]["name"] or "")[0].lower()
            cue_stem = os.path.splitext(
                os.path.basename(c["name"]))[0].lower()
            # A sheet often names a .wav the rip never shipped, so one sheet
            # beside one audio file is a match whatever either is called
            if named == stem or cue_stem == stem \
                    or (len(parsed) == 1 and len(auds) == 1):
                sheets[f["id"]] = sheet
                break
    return sheets


def cue_strms(sheet, source_url):
    """Yield (filename, slice url) for every track a CUE sheet marks out."""
    tracks = sheet["files"][0]["tracks"]
    packed = base64.urlsafe_b64encode(source_url.encode()).decode()
    for track, start, length in cue.spans(tracks):
        title = sanitize(track["title"] or f"Track {track['num']:02d}")
        q = {"u": packed, "ss": f"{start:.6f}"}
        if length is not None:
            q["t"] = f"{length:.6f}"     # the last track runs to the end
        yield (f"{track['num']:02d} - {title}.strm",
               f"{CUESLICE}/slice?" + urllib.parse.urlencode(q))


def pick_cover(files):
    """Pick the front-cover image of a music item, or None."""
    best = None
    for f in files:
        name = (f.get("short_name") or f["name"]).lower()
        if os.path.splitext(name)[1] not in IMAGE_EXT:
            continue
        size = f.get("size") or 0
        if not 4 * 1024 < size < MAX_COVER_SIZE:
            continue
        stem = os.path.basename(name)
        rank = next((i for i, p in enumerate(COVER_NAMES) if p in stem),
                    len(COVER_NAMES))
        if BACK_COVER.search(stem):
            rank += len(COVER_NAMES)
        if best is None or (rank, -size) < best[0]:
            best = ((rank, -size), f)
    return best[1] if best else None


def load_env():
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api_get(path, key, attempts=4):
    # A bare failure here reads to the prune as "this source owns nothing", so
    # the retry is load-bearing, not politeness (docs/library.md)
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
    """Yield (destination, relpath_in_library, strm_url) per playable file."""
    items = api_get(f"/{kind}/mylist?bypass_cache=true", key)
    for item in items:
        state = (item.get("download_state") or "").lower()
        if state not in ("cached", "completed", "downloaded", "seeding", "uploading") \
                and not item.get("download_present"):
            continue
        item_name = sanitize(item.get("name") or f"{kind}-{item['id']}")
        files = item.get("files") or []
        # Strip the wrapper folder the files share, not the one the item's
        # display name claims — the two disagree (docs/library.md)
        names = [f["name"] for f in files]
        top_level = None
        if names and all("/" in n for n in names):
            segs = {n.split("/", 1)[0] for n in names}
            top_level = segs.pop() if len(segs) == 1 else None
        vids = []
        auds = []
        for f in files:
            ext = os.path.splitext(f.get("short_name") or f["name"])[1].lower()
            if ext not in VIDEO_EXT and ext not in AUDIO_EXT:
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
            floor = MIN_VIDEO_SIZE if ext in VIDEO_EXT else MIN_AUDIO_SIZE
            if (f.get("size") or 0) < floor:
                # too small to be real content (a broken/placeholder upload,
                # not even a legitimate short bonus clip)
                continue
            (vids if ext in VIDEO_EXT else auds).append((f, rel_parts))

        # An item with any video is a video release, and its audio files are
        # the bundled OST rather than an album anyone asked for
        if not vids and auds:
            # Lossless first: a rip carrying both formats collapses onto one
            # path per track once the extension is stripped, and FLAC wins
            auds.sort(key=lambda fa: os.path.splitext(fa[1][-1])[1].lower()
                      not in LOSSLESS_EXT)
            sheets = match_sheets(kind, key, item["id"], files, auds)
            albums = {}
            for f, rel_parts in auds:
                sheet = sheets.get(f["id"])
                tgt = music_target(item_name, rel_parts, sheet)
                if not tgt:
                    continue
                artist, album, disc_dirs = tgt
                parts = [sanitize(artist), sanitize(album),
                         *(sanitize(d) for d in disc_dirs)]
                albums.setdefault(tuple(parts[:2]), None)
                src = strm_url(kind, key, item["id"], f["id"])
                if sheet:
                    for fname, slice_url in cue_strms(sheet, src):
                        yield ("music", [*parts, fname], slice_url)
                    continue
                yield ("music",
                       [*parts, os.path.splitext(rel_parts[-1])[0] + ".strm"],
                       src)
            cover = pick_cover(files)
            for album_parts in albums:
                if cover:
                    yield ("cover", [*album_parts, "folder.jpg"],
                           strm_url(kind, key, item["id"], cover["id"]))
            continue

        # Classify every file up front so the movie branch can see the whole
        # batch instead of deciding file-by-file: a season pack's leftover
        # bonus clips need to know a real episode already claimed this item.
        classified = [(f, rel_parts, tv_target(item_name, rel_parts))
                      for f, rel_parts in vids]

        # Absolute episode numbers borrowed from a season folder, reset to a
        # 1-based in-season sequence now the whole item is known
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
        # Size, not the name, says which files are real movies — and every
        # file clearing the bar counts, not just the biggest (docs/library.md)
        release_ids = set()
        main_titles = {}
        for f, rel_parts in non_tv:
            stem = os.path.splitext(rel_parts[-1])[0]
            title = clean_show(stem)
            # The lone video in the whole item can't be a bonus to anything
            # else — a small encode (e.g. a hardsub satellite rip) shouldn't
            # get shelved as a parentless "Featurette" for missing the bar.
            is_feature = (f.get("size") or 0) >= MAIN_FILE_MIN_SIZE \
                or len(non_tv) == 1
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
                    # An NCOP/NCED with no episode number still sits in a
                    # season folder; a raw one reads as a phantom season
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
                    # Flatten: a kept release subfolder becomes Emby's title
                    # and orphans bonus clips (docs/library.md)
                    rel_parts = [rel_parts[-1]] if is_release \
                        else ["Featurettes", rel_parts[-1]]
                    parts = [sanitize(movie_dir), *rel_parts]
            parts[-1] += ".strm"
            yield ("tv" if is_tv else "movies", parts,
                   strm_url(kind, key, item["id"], f["id"]))


def main():
    key = load_env()["TORBOX_API_KEY"]
    wanted = {}  # Path -> url
    skipped = 0
    failed = []
    covers = {}  # Path -> url of a folder.jpg to download
    roots = {"tv": LIB_TV, "movies": LIB_MOVIES, "music": LIB_MUSIC,
             "cover": LIB_MUSIC}
    for root in LIB_ROOTS:
        root.mkdir(parents=True, exist_ok=True)
    for kind in ("torrents", "usenet", "webdl"):
        try:
            for dest, parts, url in collect(kind, key):
                path = roots[dest].joinpath(*parts)
                if dest == "cover":
                    covers.setdefault(path, url)
                    continue
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

    # Album art, once per album: a music .strm carries no tags Emby can read,
    # so folder.jpg is the only cover an album folder will ever get.
    art = 0
    for path, url in covers.items():
        if path.exists() or not path.parent.is_dir() \
                or not any(path.parent.glob("*.strm")):
            continue
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "debrid-emby-stack/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read(MAX_COVER_SIZE)
            if data[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF"):
                path.write_bytes(data)
                art += 1
        except Exception as e:
            print(f"[warn] cover {path.parent.name}: {e}", file=sys.stderr)

    # Pruning is only safe when `wanted` is a complete picture of the account.
    # A source that errored contributes nothing, so every .strm it backs would
    # look stale — one dropped TLS handshake once deleted 1137 files this way.
    stale = [p for root in LIB_ROOTS
             for p in sorted(root.rglob("*.strm"), reverse=True)
             if p not in wanted]
    existing = sum(1 for root in LIB_ROOTS
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
        # A cover outlives its album's last .strm and keeps the dir alive
        for p in LIB_MUSIC.rglob("folder.jpg"):
            if not any(p.parent.glob("*.strm")):
                p.unlink()
        # prune empty dirs bottom-up
        for root in LIB_ROOTS:
            for d in sorted((d for d in root.rglob("*") if d.is_dir()),
                            key=lambda d: len(d.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass

    print(f"strm sync: {len(wanted)} wanted, {created} created, "
          f"{updated} updated, {removed} removed, {skipped} dup-skipped, "
          f"{deduped} lower-quality dupes dropped, {art} covers fetched")


if __name__ == "__main__":
    main()
