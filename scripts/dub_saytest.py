#!/usr/bin/env python3
"""Speak one line several ways, to hear which spelling the model pronounces.

The tokenizer decides pronunciation and it is not always obvious which spelling
lands: an accented letter orphans into its own token and comes out mangled, but
the ASCII fold is not automatically right either — English "cafe" is as often
"caff" as "ka-fay". The cheapest way to settle it is to hear the candidates
side by side in the voice that has to say them.

Usage:
    python3 scripts/dub_saytest.py dub/voices_test/PENGUIN.wav -o dub/clips/say \\
        "Welcome to Polar Bear Café." "Welcome to Polar Bear Cafe." \\
        "Welcome to Polar Bear Cafay."
"""

import argparse
import re
import sys
from pathlib import Path

import soundfile as sf


def slug(text, limit=44):
    return re.sub(r"[^\w ]", "", text).strip().replace(" ", "_")[:limit] or "line"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("voice", help="reference wav from the bank")
    parser.add_argument("lines", nargs="+", help="the phrasings to compare")
    parser.add_argument("-o", "--output", default="dub/clips/say")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2")
    parser.add_argument("--takes", type=int, default=2,
                        help="draws per phrasing; generation is stochastic and one "
                             "good draw of a bad spelling proves nothing (default 2)")
    args = parser.parse_args()

    from indextts.infer_v2 import IndexTTS2
    checkpoints = Path(args.checkpoints)
    tts = IndexTTS2(cfg_path=str(checkpoints / "config.yaml"),
                    model_dir=str(checkpoints), use_fp16=False)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for index, line in enumerate(args.lines, 1):
        for take in range(1, args.takes + 1):
            result = tts.infer(spk_audio_prompt=args.voice, text=line,
                               output_path=None, verbose=False)
            if result is None:
                print(f"  nothing came back for {line!r}")
                continue
            rate, samples = result
            import numpy as np
            audio = np.asarray(samples).astype("float32").reshape(-1) / 32768.0
            path = out / f"{index}-{slug(line)}-take{take}.wav"
            sf.write(path, audio, rate)
            print(f"  {path.name}  ({audio.size / rate:.1f}s)")

    print(f"\n{len(args.lines)} phrasings x {args.takes} takes in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
