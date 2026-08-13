#!/usr/bin/env python3
"""Find lines the model cannot pronounce, before spending a season saying them.

IndexTTS-2's tokenizer knows English. Anything outside it becomes an unknown
token, and the model, given a character it has no sound for, guesses — the same
word coming out "caf" one line and "cafu" the next, because it is a fresh guess
each time. It says so while it works:

    Warning: input text contains 1 unknown tokens (id=2)
    Tokens which can't be encoded:  ['É']

which is true, precise, and invisible in the middle of a model's own chatter.
This asks the same question up front, over a whole show at once, and again
after `speakable()` has folded the text, so the fix can be proven rather than
assumed.

It only tokenizes lines carrying something outside ASCII, which is what makes
it seconds rather than minutes over fifty episodes.

Usage:
    python3 scripts/dub_saycheck.py dub/work
    python3 scripts/dub_saycheck.py dub/work --show-lines
"""

import argparse
import collections
import glob
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

UNKNOWN = 2


def load_tokenizer(checkpoints):
    from indextts.utils.front import TextNormalizer, TextTokenizer
    normalizer = TextNormalizer()
    normalizer.load()
    return TextTokenizer(str(Path(checkpoints, "bpe.model")), normalizer)


def unspeakable(tokenizer, text):
    """The characters in this line the model has no token for."""
    if text.isascii():
        return []                      # nothing outside ASCII can be unknown here
    ids = tokenizer.encode(text)
    if UNKNOWN not in ids:
        return []
    return sorted({c for c in text if not c.isascii()})


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work", help="directory of *.utterances.json")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2")
    parser.add_argument("--show-lines", action="store_true",
                        help="print each affected line, not just the tally")
    args = parser.parse_args()

    from dub_render import speakable

    files = sorted(glob.glob(str(Path(args.work, "*.utterances.json"))))
    if not files:
        raise SystemExit(f"no utterance files in {args.work}")

    tokenizer = load_tokenizer(args.checkpoints)
    before, after = collections.Counter(), collections.Counter()
    hits, fixed = [], []

    for path in files:
        for utterance in json.loads(Path(path).read_text(encoding="utf-8")):
            text = utterance["text"]
            bad = unspeakable(tokenizer, text)
            if not bad:
                continue
            before.update(bad)
            hits.append((Path(path).stem, utterance["id"], text, bad))

            still = unspeakable(tokenizer, speakable(text, {}))
            if still:
                after.update(still)
                fixed.append((Path(path).stem, utterance["id"], text, still))

    def table(counts, title):
        print(f"\n{title}")
        if not counts:
            print("  none")
            return
        for char, n in counts.most_common():
            print(f"  {char!r:<8} U+{ord(char):04X}  {n:>5}x  "
                  f"{unicodedata.name(char, '?')[:40]}")

    print(f"scanned {len(files)} episodes")
    table(before, "characters the model cannot say, as the subtitles are written:")
    table(after, "still unsayable after speakable() folds the text:")

    print(f"\n{len(hits)} lines affected; {len(hits) - len(fixed)} fixed by folding, "
          f"{len(fixed)} still to answer for")
    if args.show_lines:
        for stem, ident, text, bad in (fixed or hits)[:40]:
            print(f"  {stem} #{ident} {''.join(bad)}  {text[:60]}")

    return 1 if fixed else 0


if __name__ == "__main__":
    sys.exit(main())
