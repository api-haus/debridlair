#!/usr/bin/env python3
"""Turn a fansub subtitle track into the utterance list that drives a dub.

A subtitle track is written to be read, not spoken, so it cannot be fed to a
voice model as it stands. This tool applies the corrections that a dub needs:

  - Every event is classified. A line is speech, typeset signage, or a vector
    drawing that holds no words at all. A cast dub speaks only the speech; a
    solo dub also reads the signs that fall in the clear.
  - Split utterances are rejoined. A fansubber breaks one spoken sentence over
    two subtitle events and labels only the first, so an unlabelled line
    inherits the previous speaker and merges into that utterance. One
    utterance per breath gives the voice model the prosody of a whole sentence.
  - Group lines are flagged. "EVERYONE" is several characters at once, which
    no single cloned voice can produce, so the dub leaves those in the
    original language.

`--solo` casts one voice actor to read the whole episode, which is what a
track carrying no speaker labels can still be dubbed as. Every line goes to
one bank entry; whoever the line belongs to is kept beside it as a role, and
a role only ever colours the read.

Usage:
    python3 scripts/dub_script.py EPISODE.mkv -o utterances.json
    python3 scripts/dub_script.py EPISODE.ass -o utterances.json --report
    python3 scripts/dub_script.py EPISODE.mkv -o utterances.json --solo
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

# A style name does not always say. Plenty of releases call every style
# "Default" and separate the signs by hand, and on those the placement is the
# evidence: spoken dialogue is left where the player puts it, while a sign is
# positioned on the picture, over the thing it translates. Only placement
# counts. Alignment alone does not: a fansubber raises a line to the top of
# the frame whenever two people talk at once, which is dialogue.
TYPESET = re.compile(r"\\(?:pos|move|clip|iclip|org|frz|fry|frx)\b")

# A drawing block holds vector commands rather than words. Read out, it says
# its coordinates.
DRAWING = re.compile(r"\\p[1-9]\b")

# The subtitle codecs that carry words. A bitmap track (PGS, VobSub) holds
# pictures of words, which nothing downstream can read.
TEXT_SUBTITLE_CODECS = ("ass", "ssa", "subrip", "mov_text")

# Lyrics are not signage and are never dubbed — a song stays as it was. They
# have to be named separately because a karaoke style is neither a dialogue
# style nor a sign, and a solo read that treats them as signage reads the
# opening theme out over itself.
SONG_STYLE = re.compile(r"kara|lyric|song|^insert\b|^(?:opening|ending)\b"
                        r"|^(?:op|ed)(?:\b|[_ -])", re.IGNORECASE)

# The one bank entry a solo dub speaks through. The character a line belongs
# to is kept as its role, which colours the read and never picks the voice.
SOLO_ACTOR = "NARRATOR"

# Roles that name the reading rather than a character. They are the actor's
# own register — the neutral the other roles are heard as departures from —
# so they take no colour, and they are not expected to be spoken aloud by
# anybody the way a character's name is.
PLAIN_ROLES = {"NARRATION", "NARRATOR"}

# A sign is only read when nothing is being said near it. A solo dubber reads
# out a shop front over an establishing shot and would never talk across the
# cast to do it, so a sign within this many seconds of speech is left on the
# screen where it was.
SIGN_CLEARANCE = 0.4

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
    """Pull the English subtitle track out of a Matroska file to a temp file.

    Some fansub releases never set a language tag on their sole subtitle
    track. Falling back to an untagged text track only when no track claims
    "eng"/"en" keeps an explicit tag authoritative where one exists, while
    still reading the (common) releases that skip the tag entirely.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index,codec_name:stream_tags=language,title",
         "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True)
    streams = json.loads(probe.stdout).get("streams", [])
    text_streams = [s for s in streams if s.get("codec_name") in TEXT_SUBTITLE_CODECS]

    track = next((s["index"] for s in text_streams
                  if s.get("tags", {}).get("language", "").lower() in ("eng", "en")), None)

    if track is None:
        untagged = [s for s in text_streams
                    if s.get("tags", {}).get("language", "und").lower() == "und"]
        if len(untagged) == 1:
            track = untagged[0]["index"]
        elif untagged:
            # A release shipping several untagged tracks is usually shipping
            # one with karaoke typesetting and one without. The dub wants the
            # one without: the other reads the opening theme out over itself.
            preferred = next((s for s in untagged
                              if "without karaoke" in s.get("tags", {}).get("title", "").lower()),
                             untagged[-1])
            track = preferred["index"]
            print(f"note: {video_path} tags no subtitle track as English; "
                  f"{len(untagged)} untagged text tracks found, reading stream "
                  f"{track} ({preferred.get('tags', {}).get('title', 'untitled')})",
                  file=sys.stderr)

    if track is None:
        raise SystemExit(f"no English subtitle track in {video_path}")

    destination = Path(tempfile.mkdtemp()) / "eng.ass"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video_path),
                    "-map", f"0:{track}", "-c", "copy", str(destination), "-y"],
                   check=True)
    return destination


def is_dialogue_style(style):
    return any(hint in style.lower() for hint in DIALOGUE_STYLE_HINTS)


def is_group(speaker):
    if not speaker:
        return False          # a track that named nobody named no crowd either
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


def classify(raw, style):
    """What an event is: speech, typeset signage, or not words at all.

    The style name is asked first, because a fansub that separates its styles
    has said outright which ones hold speech. Where every style is called
    "Default" the override tags still tell them apart, since a sign has to be
    drawn where the sign is.
    """
    overrides = " ".join(re.findall(r"\{([^}]*)\}", raw))
    if DRAWING.search(overrides):
        return "drawing"
    if SONG_STYLE.search(style):
        return "song"
    if TYPESET.search(overrides) or not is_dialogue_style(style):
        return "sign"
    return "speech"


def read_events(subtitle_path):
    """Read the events, classified, with each speech line's actor field.

    Resolving an unlabelled line to the previous speaker is left to the
    caller, because it is only sound where the track labels anything at all.
    On a track that labels nothing, an empty actor field is the fansubber
    saying nothing rather than saying "the same character continues", and
    reading it as continuation merges a whole scene into one breath.
    """
    events = []

    for raw in Path(subtitle_path).read_text(encoding="utf-8-sig",
                                             errors="replace").splitlines():
        if not raw.startswith("Dialogue:"):
            continue
        fields = raw[len("Dialogue:"):].split(",", 9)
        if len(fields) < 10:
            continue

        # Case is not identity: a fansub batch across many episodes writes
        # the same character's name in whatever case that release's typesetter
        # happened to use. Left as-is, "PANDA" and "Panda" become two
        # different entries everywhere downstream that keys on this string —
        # the voice bank, the render's bank lookup, tuning.json — which is
        # how one character ends up cloned from a sliver of their lines under
        # one spelling while the rest of their appearances sit under another.
        style, name = fields[3].strip(), fields[4].strip().upper()
        kind = classify(fields[9], style)
        text = clean_text(fields[9])
        if not text or kind in ("drawing", "song"):
            continue

        events.append({"start": parse_time(fields[1]), "end": parse_time(fields[2]),
                       "name": name, "kind": kind, "text": text, "style": style})

    return sorted(events, key=lambda event: event["start"])


def attribute(events):
    """Give each speech event a speaker, or say the track never named one.

    An empty actor field between two labelled ones is an explicit
    continuation and inherits. A track with no labelled line anywhere has no
    speaker to inherit from, and this says so rather than inventing one.
    """
    speech = [event for event in events if event["kind"] == "speech"]
    labelled = any(event["name"] for event in speech)

    attributed, previous_speaker = [], None
    for event in speech:
        speaker = event["name"] or (previous_speaker if labelled else None)
        if labelled and speaker is None:
            continue          # an unlabelled line before any labelled one
        previous_speaker = speaker
        attributed.append({**event, "speaker": speaker,
                           "continuation": labelled and not event["name"]})

    return attributed, labelled


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
                               "style": event["style"], "kind": event["kind"],
                               "events": 1, "inferred": 0})

    return utterances


def annotate(utterances):
    """Number the finished list and measure the room each line has.

    Runs over whatever is actually going to be spoken, signs included, so the
    room a line has accounts for everything that will be laid beside it.
    """
    utterances.sort(key=lambda utterance: utterance["start"])

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


def cast_solo(utterances):
    """Hand every line to one actor, keeping whoever said it as a role.

    This is the whole of what makes a solo dub safe to build on a track
    nobody labelled. The voice never depends on the attribution: one bank
    entry speaks the episode, and the role is a shade on the read. Get the
    role wrong and a line is coloured slightly differently; get a speaker
    wrong in a cast dub and a character is talking in somebody else's voice.
    """
    for utterance in utterances:
        utterance["role"] = utterance["speaker"] or None
        utterance["speaker"] = SOLO_ACTOR
    return utterances


def readable_signs(signs, speech):
    """The signs a solo dub can read without talking over the cast.

    Reading out what is written on the screen is part of the register: a shop
    front over an establishing shot, a letter held up to camera. It is only
    available where nobody is speaking, which is also where a sign is usually
    the only thing on screen worth saying.
    """
    readable = []
    for sign in signs:
        if any(sign["start"] - SIGN_CLEARANCE < line["end"]
               and line["start"] < sign["end"] + SIGN_CLEARANCE
               for line in speech):
            continue
        # A typesetter redraws one sign as several events to move it with the
        # shot. Read once each time it comes up, not once per event.
        if any(kept["text"] == sign["text"] and sign["start"] - kept["end"] < 15.0
               for kept in readable):
            continue
        readable.append({"start": sign["start"], "end": sign["end"],
                         "speaker": SOLO_ACTOR, "role": None, "text": sign["text"],
                         "style": sign["style"], "kind": "sign",
                         "events": 1, "inferred": 0})
    return readable


def find_overdubs(utterances):
    """Group individually-spoken lines that collide in time with another's.

    A unison line already says who speaks it together, and that mechanism
    handles it on its own. This finds the separate case where two characters'
    own, separately-written lines genuinely overlap — the mono, one-speaker-
    at-a-time render has nowhere to put the second voice but on top of the
    first, which is what `dub_overdub.py` exists to fix. Overlap is
    transitive: if A overlaps B and B overlaps C, all three are one case, even
    where A and C do not directly touch, because they still collide through B
    in the mixed-down render.
    """
    candidates = [utterance for utterance in utterances if not utterance["group"]]
    parent = {utterance["id"]: utterance["id"] for utterance in candidates}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    # Sweep in start order, checking each new line only against the lines
    # still sounding when it begins — the sorted order means nothing later
    # can overlap one that has already ended.
    open_lines = []
    for utterance in candidates:
        open_lines = [other for other in open_lines if other["end"] > utterance["start"]]
        for other in open_lines:
            if other["speaker"] != utterance["speaker"]:
                union(other["id"], utterance["id"])
        open_lines.append(utterance)

    groups = {}
    for utterance in candidates:
        groups.setdefault(find(utterance["id"]), []).append(utterance)
    cases = [group for group in groups.values() if len(group) > 1]
    cases.sort(key=lambda group: min(utterance["start"] for utterance in group))

    assignment = {}
    for case_id, group in enumerate(cases):
        for utterance in group:
            assignment[utterance["id"]] = case_id
    return assignment


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
    parser.add_argument("--overdubs", action="store_true",
                        help="print every case where separate characters' lines "
                             "collide in time, for scripts/dub_overdub.py")
    parser.add_argument("--solo", action="store_true",
                        help="cast one voice actor to read the whole episode, "
                             "keeping whoever said each line as a role")
    parser.add_argument("--no-signs", action="store_true",
                        help="do not read out typeset signage in a solo dub")
    args = parser.parse_args()

    source = Path(args.source)
    subtitle_path = extract_subtitles(source) if source.suffix.lower() in (".mkv", ".mp4") else source

    events = read_events(subtitle_path)
    speech, labelled = attribute(events)
    if not labelled and not args.solo:
        raise SystemExit(
            f"{subtitle_path} names a speaker on no line at all, so there is "
            f"nothing to cast a voice bank from. Either find a release whose "
            f"fansub fills the actor field (scripts/dub_survey.py surveys one "
            f"without downloading it), or dub this one with --solo: one voice "
            f"actor reading the whole episode.")

    utterances = merge_utterances(speech)
    if args.solo:
        cast_solo(utterances)
        if not args.no_signs:
            utterances += readable_signs([e for e in events if e["kind"] == "sign"],
                                         speech)
    annotate(utterances)

    overdubs = find_overdubs(utterances)
    for utterance in utterances:
        utterance["overdub"] = overdubs.get(utterance["id"])

    Path(args.output).write_text(json.dumps(utterances, indent=1, ensure_ascii=False))

    if args.report:
        merged = sum(1 for utterance in utterances if utterance["events"] > 1)
        groups = sum(1 for utterance in utterances if utterance["group"])
        on_screen = sum(utterance["window"] for utterance in utterances)
        print(f"{len(events)} subtitle events -> {len(utterances)} utterances "
              f"({merged} rejoined from splits)")
        if args.solo:
            read = sum(1 for utterance in utterances if utterance["kind"] == "sign")
            roles = {utterance["role"] for utterance in utterances if utterance["role"]}
            print(f"read by {SOLO_ACTOR} alone, including {read} signs that fall "
                  f"in the clear")
            print(f"{len(roles)} roles to colour the read with"
                  if roles else
                  "no roles: every line reads in the actor's own register "
                  "until scripts/dub_label.py fills them in")
        else:
            print(f"{groups} group lines left in the original language")
            print(f"{on_screen/60:.1f} min of speech across "
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

    if args.overdubs:
        cases = {}
        for utterance in utterances:
            if utterance["overdub"] is not None:
                cases.setdefault(utterance["overdub"], []).append(utterance)
        print(f"\n{len(cases)} overdub cases (separate characters' lines colliding "
              f"in time):")
        for case_id, group in sorted(cases.items()):
            group = sorted(group, key=lambda utterance: utterance["start"])
            span = f"{timestamp(group[0]['start'])}-{timestamp(group[-1]['end'])}"
            speakers = ", ".join(sorted({utterance["speaker"] for utterance in group}))
            print(f"  case {case_id}  {span}  {speakers}")
            for utterance in group:
                print(f"      {timestamp(utterance['start'])}-{timestamp(utterance['end'])}"
                      f"  {utterance['speaker']:<12} {utterance['text'][:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
