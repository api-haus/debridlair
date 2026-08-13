#!/usr/bin/env python3
"""Show how the model receives the words a season spells with accents.

On IndexTTS-2 this asked whether the tokenizer had a token for a character at
all. It often did not, and said so — `input text contains 1 unknown tokens`,
`['É']` — and the model, handed a character it has no sound for, guessed: the
same word arriving "caf" one line and "cafu" the next.

2.5's tokenizer cannot answer that question, because it cannot fail it. It is a
byte-level BPE, so every byte encodes and nothing is ever unknown. Asking it
for unknown tokens returns none for any input whatsoever, which reads as "all
clear" and means nothing at all.

What still decides pronunciation is how a word *splits*. A word the tokenizer
holds as one piece is one it has heard; a word that shatters into single
characters is one the model assembles a sound for, and that is where a guess
comes from — accented or not. `café` survives whole; `stojković` arrives in
five pieces, and the pieces are what it guesses from.

So this reports the split, beside whatever the lexicon substitutes for that
word, so a respelling can be seen to have helped rather than assumed to have.
A shattered split is a candidate for the ear, not a verdict: plenty of words
come out right in pieces. `dub_saytest.py` speaks them and `dub_sayhear.py`
transcribes them; this only decides which are worth the GPU time.

Usage:
    python3 scripts/dub_saycheck.py dub/work
    python3 scripts/dub_saycheck.py dub/work --lines
"""

import argparse
import collections
import glob
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The model lower-cases English before tokenizing and prefixes the language, so
# a word encoded any other way is not the word the model was given.
LANG_PREFIX = "<|en|> "

# Letters, plus the apostrophes that sit inside a word rather than end it.
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)

# A lexicon entry that gives phones rather than a spelling: `<göreme|G ER1 ...>`.
ANNOTATED = re.compile(r"^<[^|>]+\|[^>]+>$")


def load_tokenizer(checkpoints):
    from indextts.utils.tokenizer import get_tokenizer
    return get_tokenizer(multilingual=True, model_dir=str(checkpoints))


def split(tokenizer, text):
    """The pieces the model actually receives, and whether they rebuild the word.

    Encoded with the leading space it would carry mid-sentence: a BPE holds
    " cafe" and "cafe" as different tokens, and reporting the bare word would
    show a split the model never sees.
    """
    encoding = tokenizer.encoding
    ids = encoding.encode(" " + text, allowed_special="all")
    pieces = [encoding.decode_single_token_bytes(i).decode("utf-8", "replace")
              for i in ids]
    return pieces, encoding.decode(ids) == " " + text


def survey(work):
    """Every word and mark outside ASCII this season uses, and how often."""
    files = sorted(glob.glob(str(Path(work, "*.utterances.json"))))
    if not files:
        raise SystemExit(f"no utterance files in {work}")

    words, marks, lines = collections.Counter(), collections.Counter(), []
    for path in files:
        for utterance in json.loads(Path(path).read_text(encoding="utf-8")):
            text = utterance["text"]
            if text.isascii():
                continue
            found = [w for w in WORD.findall(text) if not w.isascii()]
            words.update(w.lower() for w in found)
            marks.update(c for c in WORD.sub(" ", text) if not c.isascii())
            lines.append((Path(path).stem, utterance["id"], text))
    return files, words, marks, lines


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work", help="directory of *.utterances.json")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2_5")
    parser.add_argument("--lexicon", metavar="JSON", default="dub/voices/lexicon.json",
                        help="the respellings to show alongside")
    parser.add_argument("--lines", action="store_true",
                        help="print each affected line, not just the inventory")
    args = parser.parse_args()

    from dub_render import speakable

    lexicon = Path(args.lexicon)
    say_as = ({word.lower(): spoken.lower()
               for word, spoken in json.loads(lexicon.read_text()).items()}
              if lexicon.exists() else {})

    tokenizer = load_tokenizer(args.checkpoints)
    files, words, marks, lines = survey(args.work)

    print(f"scanned {len(files)} episodes, {len(lines)} lines carry something "
          f"outside ASCII\n")
    print(f"tokenized as the model receives them, {LANG_PREFIX.strip()} and lower case")
    print(f"{len(say_as)} respellings from {lexicon}")

    if not words:
        print("\nno words outside ASCII")
    for word, count in words.most_common():
        spoken = speakable(word, say_as)
        pieces, whole = split(tokenizer, word)
        print(f"\n  {word!r}  {count}x")
        print(f"    as written  {' | '.join(pieces)}"
              f"{'' if whole else '   DOES NOT ROUND-TRIP'}")
        if ANNOTATED.match(spoken):
            # Tokenizing this would report on the annotation's own brackets. The
            # model never sees them: it lifts the phones out and wraps them in a
            # special token, so there is no split here to have an opinion about.
            print(f"    respelled   {spoken!r}, spoken as the phones given")
        elif spoken != word:
            said_pieces, said_whole = split(tokenizer, spoken)
            print(f"    respelled   {' | '.join(said_pieces)}   -> {spoken!r}"
                  f"{'' if said_whole else '   DOES NOT ROUND-TRIP'}")

    print("\nmarks outside ASCII, which are punctuation rather than words:")
    if not marks:
        print("  none")
    for mark, count in marks.most_common():
        plain = speakable(mark, {})
        became = "kept" if plain == mark else f"-> {plain!r}"
        print(f"  {mark!r:<8} U+{ord(mark):04X}  {count:>5}x  "
              f"{unicodedata.name(mark, '?')[:34]:<34} {became}")

    if args.lines:
        print()
        for stem, ident, text in lines[:40]:
            print(f"  {stem} #{ident}  {text[:70]}")

    # Nothing here is a failure any more: a byte-level BPE encodes whatever it
    # is given, so an exit code would only ever say "this season has accents".
    return 0


if __name__ == "__main__":
    sys.exit(main())
