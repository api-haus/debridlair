#!/usr/bin/env python3
"""Parse a CUE sheet into the album, and the tracks cut out of each FILE.

A rip stored as one FLAC plus a .cue keeps its track list only in that sheet.
`parse()` reads it; `scripts/cueslice.py` serves the cuts, and
`torbox_sync.py` writes one .strm per track pointing at them.

The sheet also carries PERFORMER and TITLE, which is the only artist and
album name a single-file rip ever states.
"""
import re

# INDEX 01 26:19:22 — minutes, seconds, and frames at 75 to the second
TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})$")
LINE = re.compile(r'^\s*(\w+)\s+(.*)$')


def _unquote(s):
    s = s.strip()
    return s[1:-1] if len(s) > 1 and s[0] == s[-1] == '"' else s


def _seconds(stamp):
    m = TIME.match(stamp.strip())
    if not m:
        return None
    mm, ss, ff = (int(g) for g in m.groups())
    return mm * 60 + ss + ff / 75.0


def decode(raw):
    """CUE sheets predate UTF-8 and a Russian tracker's are usually cp1251."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def parse(text):
    """Return {performer, title, date, files:[{name, tracks:[…]}]}."""
    album = {"performer": None, "title": None, "date": None, "files": []}
    cur_file = cur_track = None
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        word, rest = m.group(1).upper(), m.group(2)
        if word == "REM":
            bits = rest.split(None, 1)
            if len(bits) == 2 and bits[0].upper() == "DATE":
                year = re.search(r"(19|20)\d{2}", bits[1])
                album["date"] = int(year.group(0)) if year else None
        elif word == "FILE":
            name = re.match(r'\s*(".*?"|\S+)', rest)
            cur_file = {"name": _unquote(name.group(1)) if name else "",
                        "tracks": []}
            album["files"].append(cur_file)
            cur_track = None
        elif word == "TRACK":
            if cur_file is None:
                continue
            num = rest.split()[0] if rest.split() else "0"
            cur_track = {"num": int(re.sub(r"\D", "", num) or 0),
                         "title": None, "performer": None, "start": None}
            cur_file["tracks"].append(cur_track)
        elif word == "INDEX" and cur_track is not None:
            bits = rest.split()
            # INDEX 00 is the pre-gap; the track proper starts at INDEX 01
            if len(bits) == 2 and bits[0] == "01":
                cur_track["start"] = _seconds(bits[1])
            elif len(bits) == 2 and cur_track["start"] is None:
                cur_track["start"] = _seconds(bits[1])
        elif word in ("TITLE", "PERFORMER"):
            value = _unquote(rest)
            key = word.lower()
            if cur_track is not None:
                cur_track[key] = value
            elif album[key] is None:
                album[key] = value
    for f in album["files"]:
        f["tracks"] = [t for t in f["tracks"] if t["start"] is not None]
        f["tracks"].sort(key=lambda t: t["start"])
    return album


def is_image(album):
    """True when one file holds several tracks — the shape worth slicing."""
    return (len(album["files"]) == 1
            and len(album["files"][0]["tracks"]) > 1)


def spans(tracks, total=None):
    """Yield (track, start, duration) with duration None for the last one."""
    for i, t in enumerate(tracks):
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else total
        yield t, t["start"], (end - t["start"]) if end is not None else None
