#!/usr/bin/env python3
"""Turn a fansub subtitle track into the utterance list that drives a dub.

A subtitle track is written to be read, not spoken, so it cannot be fed to a
voice model as it stands. This tool applies the corrections that a dub needs:

  - Signage is dropped. Fansub styles distinguish speech from typeset graphics
    (shop signs, menus, episode titles). Only dialogue styles survive.
  - Split utterances are rejoined. A fansubber breaks one spoken sentence over
    two subtitle events and labels only the first, so an unlabelled line
    inherits the previous speaker and merges into that utterance. One
    utterance per breath gives the voice model the prosody of a whole sentence.
  - Group lines are flagged. "EVERYONE" is several characters at once, which
    no single cloned voice can produce, so the dub leaves those in the
    original language.

Usage:
    python3 scripts/dub_script.py EPISODE.mkv -o utterances.json
    python3 scripts/dub_script.py EPISODE.ass -o utterances.json --report
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Fansub style names carry meaning: dialogue styles hold speech, everything
# else is typeset signage that must never be dubbed. Matched case-insensitively
# as a substring of the style name.
DIALOGUE_STYLE_HINTS = ("main", "dialog", "default", "overlap", "internal",
                        "flashback", "thought", "italics", "alt", "caption")

# Speaker labels that name a crowd rather than a character. A cloned voice
# cannot produce these, so they stay in the original audio.
GROUP_LABELS = {"everyone", "all", "kids", "both", "both bears", "crowd",
                "group", "together", "various", "many"}

# Two events belong to one utterance when the gap between them is under this,
# in seconds, and the speaker has not changed.
MERGE_GAP = 0.6

# A merge is only allowed when the earlier text does not already end a
# sentence, unless the later event was unlabelled (an explicit continuation).
SENTENCE_END = re.compile(r'[.!?…。！？]["”’)]?\s*$')


def parse_time(stamp):
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_subtitles(video_path):
    """Pull the English subtitle track out of a Matroska file."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index:stream_tags=language",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, check=True)

    for row in probe.stdout.splitlines():
        parts = row.split(",")
        if len(parts) >= 2 and parts[1].strip().lower() in ("eng", "en"):
            destination = Path(tempfile.mkdtemp()) / "eng.ass"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(video_path),
                            "-map", f"0:{parts[0]}", "-c", "copy",
                            str(destination), "-y"], check=True)
            return destination
    raise SystemExit(f"no English subtitle track in {video_path}")


def is_dialogue_style(style):
    return any(hint in style.lower() for hint in DIALOGUE_STYLE_HINTS)


def is_group(speaker):
    lowered = speaker.lower().strip()
    return lowered in GROUP_LABELS or "/" in lowered or "&" in lowered


def clean_text(raw):
    """Strip ASS override tags and drawing commands, unwrap manual breaks."""
    text = re.sub(r"\{[^}]*\}", "", raw)
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    return re.sub(r"\s+", " ", text).strip()


def read_events(subtitle_path):
    """Read dialogue events, resolving unlabelled lines to their speaker."""
    events = []
    previous_speaker = None

    for raw in Path(subtitle_path).read_text(encoding="utf-8-sig",
                                             errors="replace").splitlines():
        if not raw.startswith("Dialogue:"):
            continue
        fields = raw[len("Dialogue:"):].split(",", 9)
        if len(fields) < 10:
            continue

        style, name = fields[3].strip(), fields[4].strip()
        text = clean_text(fields[9])
        if not text or not is_dialogue_style(style):
            continue

        speaker = name or previous_speaker
        if speaker is None:
            continue          # an unlabelled line before any labelled one
        previous_speaker = speaker

        events.append({"start": parse_time(fields[1]), "end": parse_time(fields[2]),
                       "speaker": speaker, "text": text, "continuation": not name})

    return sorted(events, key=lambda event: event["start"])


def merge_utterances(events):
    """Join subtitle events that are one spoken sentence into one utterance."""
    utterances = []

    for event in events:
        joinable = (
            utterances
            and utterances[-1]["speaker"] == event["speaker"]
            and event["start"] - utterances[-1]["end"] <= MERGE_GAP
            and (event["continuation"]
                 or not SENTENCE_END.search(utterances[-1]["text"]))
        )
        if joinable:
            utterances[-1]["text"] += " " + event["text"]
            utterances[-1]["end"] = event["end"]
            utterances[-1]["events"] += 1
        else:
            utterances.append({"start": event["start"], "end": event["end"],
                               "speaker": event["speaker"], "text": event["text"],
                               "events": 1})

    for index, utterance in enumerate(utterances):
        utterance["id"] = index
        utterance["group"] = is_group(utterance["speaker"])
        # How much room the line has before the next speaker starts. The dub
        # may run past its own subtitle window into this slack without
        # colliding, which is what keeps natural pacing possible.
        following = next((other for other in utterances[index + 1:]
                          if other["start"] >= utterance["end"]), None)
        utterance["slack"] = round((following["start"] - utterance["end"])
                                   if following else 5.0, 3)
        utterance["window"] = round(utterance["end"] - utterance["start"], 3)

    return utterances


def find_scenes(utterances, count):
    """Rank continuous stretches of dialogue by how many characters speak.

    A preview is only informative when it shows several cloned voices talking
    to each other, because the thing to judge is whether the characters sound
    distinct. A scene breaks wherever the dialogue stops for a while.
    """
    scenes, current = [], []
    for utterance in utterances:
        if current and utterance["start"] - current[-1]["end"] > 4.0:
            scenes.append(current)
            current = []
        current.append(utterance)
    if current:
        scenes.append(current)

    ranked = []
    for scene in scenes:
        speakers = {utterance["speaker"] for utterance in scene
                    if not utterance["group"]}
        ranked.append({"start": scene[0]["start"], "end": scene[-1]["end"],
                       "speakers": sorted(speakers), "lines": len(scene)})

    ranked.sort(key=lambda scene: (-len(scene["speakers"]), -scene["lines"]))
    return ranked[:count]


def timestamp(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="an .mkv, or an .ass/.srt subtitle file")
    parser.add_argument("-o", "--output", required=True, help="utterance JSON to write")
    parser.add_argument("--report", action="store_true", help="print a summary")
    parser.add_argument("--scenes", type=int, metavar="N", default=0,
                        help="also rank the N best multi-character scenes to preview")
    args = parser.parse_args()

    source = Path(args.source)
    subtitle_path = extract_subtitles(source) if source.suffix.lower() in (".mkv", ".mp4") else source

    events = read_events(subtitle_path)
    utterances = merge_utterances(events)
    Path(args.output).write_text(json.dumps(utterances, indent=1, ensure_ascii=False))

    if args.report:
        merged = sum(1 for utterance in utterances if utterance["events"] > 1)
        groups = sum(1 for utterance in utterances if utterance["group"])
        speech = sum(utterance["window"] for utterance in utterances)
        print(f"{len(events)} subtitle events -> {len(utterances)} utterances "
              f"({merged} rejoined from splits)")
        print(f"{groups} group lines left in the original language")
        print(f"{speech/60:.1f} min of speech across "
              f"{len({utterance['speaker'] for utterance in utterances})} speakers")
        print(f"\nwrote {args.output}")

    if args.scenes:
        print(f"\nbest scenes to preview:")
        for scene in find_scenes(utterances, args.scenes):
            span = f"{timestamp(scene['start'])}-{timestamp(scene['end'])}"
            print(f"  {span}  {len(scene['speakers'])} chars, {scene['lines']:>3} lines"
                  f"  {', '.join(scene['speakers'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
