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

# What separates the members of a group label that names them.
GROUP_SEPARATOR = re.compile(r"\s*[/&+]\s*|\s+and\s+", re.IGNORECASE)

# Two events belong to one utterance when the gap between them is under this,
# in seconds, and the speaker has not changed.
MERGE_GAP = 0.6

# How many other speakers may sit between the halves of one split sentence.
# Overlapping dialogue puts an interruption in the middle of a line, and past
# a couple of intervening speakers a rejoin is more likely to be wrong than
# right.
INTERLEAVE_DEPTH = 3

# A merge is only allowed when the earlier text does not already end a
# sentence, unless the later event was unlabelled (an explicit continuation).
SENTENCE_END = re.compile(r'[.!?…。！？]["”’)]?\s*$')

# The half that continues a sentence cannot be the start of one. A lowercase
# opening, or a mark that only ever appears mid-sentence, is what distinguishes
# a genuine continuation from the same character simply speaking again.
CONTINUES_SENTENCE = re.compile(r'^["“\'(\[]?(?:[a-z]|[,;:—–-])')


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


def named_members(speaker):
    """The characters a group label names, when it names any.

    "BEAR/PENGUIN" says exactly who is speaking, so the line can be spoken by
    each of them and laid together. "EVERYONE" and "BOTH BEARS" name nobody,
    and there is no way to know who to cast, so those stay in the original
    audio. Splitting the label is all that happens here; matching the names to
    banked voices belongs where the bank is known.
    """
    parts = [part.strip() for part in GROUP_SEPARATOR.split(speaker) if part.strip()]
    return parts if len(parts) > 1 else []


def clean_text(raw):
    """Strip ASS override tags and drawing commands, unwrap manual breaks.

    A trailing lowercase parenthetical is a translator's gloss, not speech.
    Subtitles carry them so a reader can see the Japanese a pun turns on —
    "That would be the daily special (higawari)" — and reading one aloud says
    a romaji word to an audience that came for English. Only trailing ones
    with no capital letter are taken, which is the shape a gloss has and an
    ordinary spoken aside does not.
    """
    text = re.sub(r"\{[^}]*\}", "", raw)
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    text = re.sub(r"\s*\(([a-z][a-z \-']*)\)\s*([.!?…]?)\s*$", r"\2", text)
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
                       "speaker": speaker, "text": text, "style": style,
                       "continuation": not name})

    return sorted(events, key=lambda event: event["start"])


def joins_sentence(earlier, later):
    """Is the later event the rest of the earlier one's sentence?

    Failing to merge costs a cold start in the middle of a sentence, which is
    merely worse. Merging two separate sentences produces a run-on read in one
    breath and leaves both lines in the wrong slot, which is wrong. So this
    demands evidence and abstains without it.

    An empty actor field is the fansubber saying outright that the same
    character continues, and is trusted alone. Absent that, the two halves must
    agree with each other — the first leaving its sentence open and the second
    unable to begin one — and must share a style, since a style change means a
    different context rather than a continued line.
    """
    if later["continuation"]:
        return True
    return (later["style"] == earlier["style"]
            and not SENTENCE_END.search(earlier["text"])
            and bool(CONTINUES_SENTENCE.match(later["text"])))


def merge_utterances(events):
    """Join subtitle events that are one spoken sentence into one utterance.

    The half of a split sentence is not always the previous event. Characters
    talk over each other, so a fansubber puts the interrupting line between the
    two halves — one character's aside is written around another's. Looking
    only at the previous utterance leaves the sentence in two pieces, and each
    piece is then generated as its own cold start with its own intonation,
    which is audible as one speaker saying two disconnected fragments.
    """
    utterances = []

    for event in events:
        # Search back past whoever spoke in between, but only as far as a
        # sentence that is still open and still close in time.
        target = None
        for candidate in reversed(utterances[-INTERLEAVE_DEPTH:]):
            if candidate["speaker"] != event["speaker"]:
                continue
            if event["start"] - candidate["end"] > MERGE_GAP:
                break
            if joins_sentence(candidate, event):
                target = candidate
            break

        if target is not None:
            target["text"] += " " + event["text"]
            target["end"] = max(target["end"], event["end"])
            target["events"] += 1
            # Merges driven by orthography rather than by the actor field are
            # the ones an audit needs to look at.
            target["inferred"] += 0 if event["continuation"] else 1
        else:
            utterances.append({"start": event["start"], "end": event["end"],
                               "speaker": event["speaker"], "text": event["text"],
                               "style": event["style"], "events": 1,
                               "inferred": 0})

    for index, utterance in enumerate(utterances):
        utterance["id"] = index
        utterance["group"] = is_group(utterance["speaker"])
        utterance["members"] = named_members(utterance["speaker"]) if utterance["group"] else []
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
    parser.add_argument("--audit", action="store_true",
                        help="print every rejoin that was inferred rather than "
                             "stated by the actor field, for eyeballing")
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

    if args.audit:
        # Rejoins taken from the actor field are the fansubber's own word and
        # need no review. These were inferred from how the text reads, so they
        # are the ones that could be wrong, and the only way to know is to look.
        guessed = [u for u in utterances if u["inferred"]]
        print(f"\n{sum(u['inferred'] for u in guessed)} rejoins inferred from the text "
              f"(the rest came from the actor field):")
        for utterance in guessed:
            print(f"  {utterance['speaker']:<12} {utterance['text'][:96]}")

    if args.scenes:
        print(f"\nbest scenes to preview:")
        for scene in find_scenes(utterances, args.scenes):
            span = f"{timestamp(scene['start'])}-{timestamp(scene['end'])}"
            print(f"  {span}  {len(scene['speakers'])} chars, {scene['lines']:>3} lines"
                  f"  {', '.join(scene['speakers'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
