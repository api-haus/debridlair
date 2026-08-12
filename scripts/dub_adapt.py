#!/usr/bin/env python3
"""Find the lines no delivery can fit, and collect shorter ones.

A subtitle is written to be read in the time it is on screen. Read aloud, the
same words often take longer than the shot allows, and English translated from
Japanese is usually the longer of the two. Past a point this is not a timing
problem: the line cannot be spoken that fast by anyone, and squeezing it
harder only produces a gabble.

Dubbing studios have always answered this by rewriting rather than translating
— the trade calls it adaptation. This tool does the flagging and the
bookkeeping; the rewriting is a language task, done by whoever or whatever is
good at language, and handed back through a file.

The threshold is measured, not assumed. A previous render reports how long the
voice model took over each line, which gives the rate it actually speaks at.
Anything demanding more than that rate times the compression ceiling cannot be
delivered, however the timing is arranged.

    # 1. flag them, with context and a word budget
    python3 scripts/dub_adapt.py dub/work/s01e01.utterances.json \\
        --timing dub/preview/s01e01.dubbed.mkv.timing.json

    # 2. fill in "adapted" in the file it writes, then re-render
    # 3. check what the rewrite bought
    python3 scripts/dub_adapt.py dub/work/s01e01.utterances.json --review
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

# The ceiling dub_render will compress to. A line needing more than the model's
# own rate times this cannot be delivered at any timing.
MAX_COMPRESSION = 1.35

# Rewrites aim below the ceiling rather than at it, so an adapted line still
# has room to be nudged rather than arriving already at the limit.
TARGET_HEADROOM = 1.15

# Used when no render has been measured yet. Replaced by the real figure as
# soon as a timing report exists.
ASSUMED_RATE = 2.3


def speaking_rate(timing_path, utterances):
    """How fast the voice model actually speaks, in words per second."""
    if not timing_path or not Path(timing_path).exists():
        return ASSUMED_RATE, 0

    by_id = {u["id"]: u for u in utterances}
    rates = []
    for row in json.loads(Path(timing_path).read_text()):
        utterance = by_id.get(row["id"])
        if utterance is None or row.get("generated", 0) <= 0.5:
            continue
        words = len(utterance["text"].split())
        if words >= 3:
            rates.append(words / row["generated"])
    return (statistics.median(rates), len(rates)) if rates else (ASSUMED_RATE, 0)


def room_for(utterance):
    """Seconds the line may occupy before it collides with the next speaker."""
    return utterance["window"] + max(0.0, utterance["slack"] - 0.15)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("utterances")
    parser.add_argument("--timing", help="a timing.json from a previous render")
    parser.add_argument("-o", "--output", help="where to collect rewrites "
                                               "(default beside the utterances)")
    parser.add_argument("--review", action="store_true",
                        help="report on rewrites already collected")
    args = parser.parse_args()

    utterance_path = Path(args.utterances)
    utterances = json.loads(utterance_path.read_text())
    output = Path(args.output) if args.output else utterance_path.with_name(
        utterance_path.name.replace(".utterances.json", ".adaptations.json"))

    rate, sampled = speaking_rate(args.timing, utterances)
    deliverable = rate * MAX_COMPRESSION
    existing = json.loads(output.read_text()) if output.exists() else {}

    # Rewrites are carried over by what the line said, not by its id. An id is
    # a position in the utterance list, so re-parsing the subtitles renumbers
    # them and a rewrite would land on somebody else's line.
    carried = {(entry["speaker"], entry["original"]): entry["adapted"]
               for entry in existing.values() if entry.get("adapted")}

    if args.review:
        done = {key: entry for key, entry in existing.items() if entry.get("adapted")}
        print(f"{len(done)} of {len(existing)} lines rewritten")
        for key, entry in done.items():
            room = entry["room"]
            before = len(entry["original"].split()) / room
            after = len(entry["adapted"].split()) / room
            flag = "" if after <= deliverable else "   still too long"
            print(f"\n  {entry['speaker']}  {before:.1f} -> {after:.1f} w/s{flag}")
            print(f"    was: {entry['original']}")
            print(f"    now: {entry['adapted']}")
        return 0

    by_time = sorted(utterances, key=lambda u: u["start"])
    position = {u["id"]: index for index, u in enumerate(by_time)}

    collected = {}
    for utterance in utterances:
        if utterance["group"]:
            continue
        room = room_for(utterance)
        words = len(utterance["text"].split())
        if room <= 0.2 or words / room <= deliverable:
            continue

        index = position[utterance["id"]]
        key = str(utterance["id"])
        collected[key] = {
            "speaker": utterance["speaker"],
            "original": utterance["text"],
            # A rewrite has to say the same thing to the same people, so the
            # lines either side travel with it.
            "before": [u["text"] for u in by_time[max(0, index - 2):index]],
            "after": [u["text"] for u in by_time[index + 1:index + 3]],
            "room": round(room, 2),
            "words": words,
            "target_words": max(2, int(room * rate * TARGET_HEADROOM)),
            "adapted": carried.get((utterance["speaker"], utterance["text"]), ""),
        }

    output.write_text(json.dumps(collected, indent=1, ensure_ascii=False))

    source = f"measured over {sampled} lines" if sampled else "assumed, no render measured yet"
    print(f"model speaks at {rate:.2f} words/sec ({source})")
    print(f"deliverable at the {MAX_COMPRESSION} ceiling: {deliverable:.2f} words/sec\n")
    print(f"{len(collected)} of {len(utterances)} lines cannot be delivered as written:\n")
    for entry in sorted(collected.values(),
                        key=lambda e: -len(e["original"].split()) / e["room"])[:12]:
        current = len(entry["original"].split()) / entry["room"]
        print(f"  {entry['speaker']:<12}{current:>4.1f} w/s  "
              f"{entry['words']:>2} words -> {entry['target_words']:>2}   {entry['original'][:46]}")

    print(f"\nwrote {output}")
    print("Fill in \"adapted\" for each, keeping the meaning and the register, "
          "then re-render.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
