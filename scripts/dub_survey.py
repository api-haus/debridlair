#!/usr/bin/env python3
"""Survey a title's subtitle track to decide whether it can be auto-dubbed.

An auto-dub keeps each character's own voice only when the subtitle track says
who speaks each line, and only when that character has enough clean, solo
speech to clone from. This tool answers both questions before any GPU time is
spent, so a title that cannot be dubbed well is rejected in seconds.

Usage:
    python3 scripts/dub_survey.py EPISODE.ass [EPISODE.ass ...]
    python3 scripts/dub_survey.py --mkv EPISODE.mkv        # extracts English ASS first

Read the verdict column. A title whose main cast reaches "usable" or better is
worth dubbing. A title where every row is "fallback" has no speaker labels, so
the dub needs speaker diarization and the result is poor. Pass several episodes
to pool reference audio across a season, which is how the supporting cast gets
enough material.
"""

import argparse
import collections
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Zero-shot voice cloning needs a few seconds of reference audio. These
# thresholds are total clean solo speech per character, in seconds.
STRONG, USABLE, THIN = 30.0, 10.0, 3.0

# A reference clip shorter than this is too short to clone a timbre from.
MIN_CLIP = 1.5

# Fansub styles carry meaning: dialogue styles hold speech, everything else is
# typeset signage (shop signs, menus, episode titles) that must never be dubbed.
# Matched case-insensitively as a substring of the style name.
DIALOGUE_STYLE_HINTS = ("main", "dialog", "default", "overlap", "internal",
                        "flashback", "thought", "italics", "alt", "caption")


def parse_time(stamp):
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_ass(mkv_path):
    """Pull the English ASS/SRT track out of a Matroska file to a temp file."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index:stream_tags=language",
         "-of", "csv=p=0", str(mkv_path)],
        capture_output=True, text=True, check=True)

    track = None
    for row in probe.stdout.splitlines():
        parts = row.split(",")
        if len(parts) >= 2 and parts[1].strip().lower() in ("eng", "en"):
            track = int(parts[0])
            break
    if track is None:
        raise SystemExit(f"no English subtitle track in {mkv_path}")

    out = Path(tempfile.mkdtemp()) / "eng.ass"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(mkv_path),
                    "-map", f"0:{track}", "-c", "copy", str(out), "-y"],
                   check=True)
    return out


def is_dialogue_style(style):
    lowered = style.lower()
    return any(hint in lowered for hint in DIALOGUE_STYLE_HINTS)


def read_lines(path):
    """Return the speech events as (start, end, speaker, text, is_continuation).

    Signage styles are dropped. An unlabelled dialogue line inherits the
    previous speaker, because fansubbers split one utterance across two
    subtitle events and label only the first.
    """
    events = []
    previous_speaker = None

    for raw in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not raw.startswith("Dialogue:"):
            continue
        fields = raw[len("Dialogue:"):].split(",", 9)
        if len(fields) < 10:
            continue

        start, end, style, name, text = (parse_time(fields[1]), parse_time(fields[2]),
                                         fields[3].strip(), fields[4].strip(), fields[9])
        text = re.sub(r"\{[^}]*\}", "", text).replace(r"\N", " ").strip()
        if not text or not is_dialogue_style(style):
            continue

        continuation = not name
        speaker = name or previous_speaker
        if speaker is None:
            continue
        previous_speaker = speaker
        events.append((start, end, speaker, text, continuation))

    return events


def survey(paths):
    stats = collections.defaultdict(
        lambda: {"lines": 0, "speech": 0.0, "clean": 0, "clean_speech": 0.0})
    total_lines = 0

    for path in paths:
        events = read_lines(path)
        total_lines += len(events)
        for index, event in enumerate(events):
            start, end, speaker = event[0], event[1], event[2]
            entry = stats[speaker]
            entry["lines"] += 1
            entry["speech"] += end - start

            # A clip is usable as a voice reference only when nobody else talks
            # over it, because an overlapping voice poisons the cloned timbre.
            solo = not any(start < other[1] and other[0] < end and other[2] != speaker
                           for position, other in enumerate(events) if position != index)
            if solo and end - start >= MIN_CLIP:
                entry["clean"] += 1
                entry["clean_speech"] += end - start

    return stats, total_lines


def verdict(clean_speech):
    if clean_speech >= STRONG:
        return "strong clone"
    if clean_speech >= USABLE:
        return "usable clone"
    if clean_speech >= THIN:
        return "thin, pool more episodes"
    return "fallback voice"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", help="ASS/SRT subtitle files, or MKVs with --mkv")
    parser.add_argument("--mkv", action="store_true",
                        help="treat the sources as Matroska files and extract the English track")
    args = parser.parse_args()

    paths = [extract_ass(source) for source in args.sources] if args.mkv else args.sources
    stats, total_lines = survey(paths)

    if not stats:
        raise SystemExit("no labelled dialogue found: this title needs speaker diarization")

    print(f"{'CHARACTER':<18}{'lines':>6}{'speech':>9}{'clean':>7}{'usable':>9}  verdict")
    print("-" * 80)
    for speaker, entry in sorted(stats.items(), key=lambda item: -item[1]["clean_speech"]):
        print(f"{speaker:<18}{entry['lines']:>6}{entry['speech']:>8.0f}s"
              f"{entry['clean']:>7}{entry['clean_speech']:>8.0f}s  {verdict(entry['clean_speech'])}")

    cloneable = sum(1 for entry in stats.values() if entry["clean_speech"] >= USABLE)
    dubbed_speech = sum(entry["speech"] for entry in stats.values())
    print(f"\n{total_lines} dialogue lines, {dubbed_speech/60:.0f} min of speech, "
          f"{len(stats)} speakers, {cloneable} with enough audio to clone")


if __name__ == "__main__":
    sys.exit(main())
