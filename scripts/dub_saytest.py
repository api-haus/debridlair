#!/usr/bin/env python3
"""Speak one line several ways, to hear which spelling the model pronounces.

The tokenizer decides pronunciation and it is not always obvious which spelling
lands: an accented letter orphans into its own token and comes out mangled, but
the ASCII fold is not automatically right either — English "cafe" is as often
"caff" as "ka-fay", and folding "résumé" spells a different word entirely.
2.5 offers a third candidate, an inline respelling in ARPABET the model is
trained to obey: `<café|K AH0 F EY1>`. The cheapest way to settle which to use
is to hear them side by side in the voice that has to say them.

A line may name its own voice, because a season's hard words are spread across
its cast and loading the model costs more than every line in this file put
together. `PENGUIN:: line` draws from the bank; anything else uses the voice
given on the command line.

Usage:
    python3 scripts/dub_saytest.py PENGUIN -o dub/clips/say \\
        "Welcome to Polar Bear Café." "Welcome to Polar Bear Cafe." \\
        "SLOTH:: Here's my résumé..."
"""

import argparse
import json
import re
import sys
from pathlib import Path

import soundfile as sf

VOICES = Path("dub/voices")

# Two colons, because one is punctuation a line of dialogue may well open with.
NAMED = re.compile(r"^([A-Z][A-Z0-9_]*)::\s*(.+)$", re.DOTALL)


def slug(text, limit=44):
    return re.sub(r"[^\w ]", "", text).strip().replace(" ", "_")[:limit] or "line"


def voice_path(name):
    """A bank name or a path to a wav, whichever was given."""
    direct = Path(name)
    if direct.suffix == ".wav":
        return direct
    return VOICES / f"{name}.wav"


def parse(line, fallback):
    match = NAMED.match(line)
    if not match:
        return fallback, line
    name, text = match.groups()
    path = voice_path(name)
    if not path.exists():
        raise SystemExit(f"{line[:40]!r} asks for {name}, and {path} does not exist")
    return path, text


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("voice", help="bank name or wav, for lines that name none")
    parser.add_argument("lines", nargs="+",
                        help="the phrasings to compare, each optionally 'NAME:: line'")
    parser.add_argument("-o", "--output", default="dub/clips/say")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2_5")
    parser.add_argument("--takes", type=int, default=2,
                        help="draws per phrasing; generation is stochastic and one "
                             "good draw of a bad spelling proves nothing (default 2)")
    args = parser.parse_args()

    # Resolved before the model loads: a name typed wrong is worth hearing about
    # now rather than after a minute of weights.
    fallback = voice_path(args.voice)
    if not fallback.exists():
        raise SystemExit(f"{fallback} does not exist")
    spoken = [parse(line, fallback) for line in args.lines]

    from indextts.infer_v2_5 import IndexTTS2
    checkpoints = Path(args.checkpoints)
    tts = IndexTTS2(cfg_path=str(checkpoints / "config.yaml"),
                    model_dir=str(checkpoints), use_bf16=False)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    # What was said is written down before anything is spoken, because the
    # filenames cannot carry it: the whole point of the exercise is comparing
    # spellings, and a slug of two candidate spellings differs by an accent
    # somewhere in the middle. Forty files named that way are not a result
    # anybody can read.
    index_path = out / "said.json"
    index_path.write_text(json.dumps(
        [{"n": n, "voice": voice.stem, "text": line}
         for n, (voice, line) in enumerate(spoken, 1)],
        indent=1, ensure_ascii=False), encoding="utf-8")

    for index, (voice, line) in enumerate(spoken, 1):
        for take in range(1, args.takes + 1):
            result = tts.infer(spk_audio_prompt=str(voice), text=line,
                               output_path=None, lang="EN", verbose=False)
            if result is None:
                print(f"  nothing came back for {line!r}")
                continue
            rate, samples = result
            import numpy as np
            audio = np.asarray(samples).astype("float32").reshape(-1) / 32768.0
            path = out / f"{index:02d}-{voice.stem}-{slug(line)}-take{take}.wav"
            sf.write(path, audio, rate)
            print(f"  {path.name}  ({audio.size / rate:.1f}s)")

    print(f"\n{len(args.lines)} phrasings x {args.takes} takes in {out}")
    print(f"what each number says is in {index_path.name}:")
    for number, (voice, line) in enumerate(spoken, 1):
        print(f"  {number:02d}  {voice.stem:<12} {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
