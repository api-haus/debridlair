#!/usr/bin/env python3
"""Transcribe a pronunciation test, so the verdict is not one person's ear.

`dub_saytest.py` speaks a word several ways and leaves forty files behind. Which
of them is right is a judgement, and a judgement made once at midnight over a
laptop speaker is not one a season should be rendered on. Whisper heard the same
audio without knowing which spelling produced it, so what it writes down is
evidence: a clip transcribed "cafe" was said "ka-fay", and a clip transcribed
"caff latte" was not.

It does not settle the question. A transcript agrees with the ear or it argues
with it, and either is worth more than the ear alone — this exists so the
argument happens before the GPU time, not after.

Takes of one phrasing are printed together, because generation is stochastic and
a single good draw of a bad spelling is the trap the whole exercise is avoiding.

Usage:
    python3 scripts/dub_sayhear.py dub/clips/pronunciation_2_5
    python3 scripts/dub_sayhear.py dub/clips/pronunciation_2_5 --model medium.en
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

# Files land as `01-POLAR_BEAR-Welcome_to_-take1.wav`; the index groups the
# takes of one phrasing and the voice is worth printing beside them.
CLIP = re.compile(r"^(\d+)-([A-Z][A-Z0-9_]*)-(.+)-take(\d+)$")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", help="a directory dub_saytest.py wrote")
    parser.add_argument("--model", default="small.en",
                        help="whisper model; medium.en is more faithful and slower")
    parser.add_argument("--word", action="append", default=[],
                        help="highlight lines whose transcript is missing this word; "
                             "repeatable")
    args = parser.parse_args()

    root = Path(args.directory)
    clips = sorted(root.glob("*.wav"))
    if not clips:
        raise SystemExit(f"no wav files in {root}")

    # The line as it was typed, which the filename cannot hold: a slug drops the
    # punctuation and the annotation brackets, and two candidate spellings slug
    # to very nearly the same thing.
    said = root / "said.json"
    lines = ({f"{entry['n']:02d}": entry["text"]
              for entry in json.loads(said.read_text(encoding="utf-8"))}
             if said.exists() else {})

    import whisper
    model = whisper.load_model(args.model)

    grouped = collections.defaultdict(list)
    for clip in clips:
        match = CLIP.match(clip.stem)
        key = (match.group(1), match.group(2), match.group(3)) if match else \
              ("--", "?", clip.stem)
        # fp16 is off because these run on CPU as readily as GPU and a warning
        # per clip buries the transcripts this exists to show.
        heard = model.transcribe(str(clip), fp16=False, language="en")
        grouped[key].append(heard["text"].strip())

    wanted = [w.lower() for w in args.word]
    for (index, voice, phrasing), takes in sorted(grouped.items()):
        said_as = lines.get(index, phrasing.replace("_", " "))
        print(f"\n{index}  {voice}  said:  {said_as}")
        for number, text in enumerate(takes, 1):
            missing = [w for w in wanted if w not in text.lower()]
            flag = f"   MISSING {' '.join(missing)}" if missing else ""
            print(f"    take{number}  heard: {text}{flag}")

    if not lines:
        print(f"\n{root/'said.json'} is missing, so the lines above are "
              f"filenames rather than what was typed")
    print(f"\n{len(clips)} clips through {args.model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
