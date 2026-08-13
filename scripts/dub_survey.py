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
worth dubbing as a cast, one cloned voice per character. Pass several episodes
to pool reference audio across a season, which is how the supporting cast gets
enough material.

A title whose track names nobody cannot be dubbed that way at all, and this
says so in as many words rather than printing an empty table. It is still
dubbable as a solo read — one actor for the whole episode, the way an amateur
dub has always been made — which is what `dub_script.py --solo` builds.
"""

import argparse
import collections
import sys
from pathlib import Path

# The survey has to measure what the dub will actually speak, so it reads the
# track through the same parser the dub does rather than a second copy of the
# rules. A survey and a render that disagree about what counts as a line is a
# survey that tells you about a different episode.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_script import (attribute, extract_subtitles, read_events,  # noqa: E402
                        readable_signs)

# Zero-shot voice cloning needs a few seconds of reference audio. These
# thresholds are total clean solo speech per character, in seconds.
STRONG, USABLE, THIN = 30.0, 10.0, 3.0

# A reference clip shorter than this is too short to clone a timbre from.
MIN_CLIP = 1.5


def survey(paths):
    """Measure each named character, and everything the track left unnamed.

    A track that names nobody puts every line in the unnamed bucket, which is
    the whole finding: there is no cast to build, and the numbers that matter
    then are how much clean speech there is to cut one reference from and how
    much of the episode is two people at once.
    """
    stats = collections.defaultdict(
        lambda: {"lines": 0, "speech": 0.0, "clean": 0, "clean_speech": 0.0})
    unnamed = {"lines": 0, "speech": 0.0, "clean_speech": 0.0, "collisions": 0,
               "signs": 0, "labelled_files": 0}

    for path in paths:
        events = read_events(path)
        speech, labelled = attribute(events)
        unnamed["labelled_files"] += 1 if labelled else 0
        unnamed["signs"] += len(readable_signs([e for e in events if e["kind"] == "sign"],
                                               speech))

        for index, event in enumerate(speech):
            start, end, speaker = event["start"], event["end"], event["speaker"]

            # A clip is usable as a voice reference only when nobody else talks
            # over it, because an overlapping voice poisons the cloned timbre.
            # On an unnamed track there is no way to know who the overlapping
            # voice belongs to, so any overlap at all disqualifies the clip.
            solo = not any(start < other["end"] and other["start"] < end
                           and (speaker is None or other["speaker"] != speaker)
                           for position, other in enumerate(speech) if position != index)

            if speaker is None:
                unnamed["lines"] += 1
                unnamed["speech"] += end - start
                unnamed["collisions"] += 0 if solo else 1
                unnamed["clean_speech"] += (end - start) if solo and end - start >= MIN_CLIP else 0
                continue

            entry = stats[speaker]
            entry["lines"] += 1
            entry["speech"] += end - start
            if solo and end - start >= MIN_CLIP:
                entry["clean"] += 1
                entry["clean_speech"] += end - start

    return stats, unnamed


def report_unnamed(unnamed, files):
    """Say what a track that names nobody can still be made into.

    An empty cast table is not the same finding as a thin one, and printing
    nothing invites the reader to conclude the tool failed. What is true here
    is narrower and more useful: no character can be cloned, and the episode
    is still dubbable by one actor reading all of it.
    """
    if not unnamed["lines"]:
        raise SystemExit("no dialogue found at all: this is not a subtitle track "
                         "the dub can read")

    print(f"no character is named on any line of {files} file(s) — "
          f"there is no cast to clone\n")
    print(f"  {unnamed['lines']:>5} dialogue lines")
    print(f"  {unnamed['speech'] / 60:>5.1f} min of speech")
    print(f"  {unnamed['collisions']:>5} lines overlap another and would be read in sequence")
    print(f"  {unnamed['signs']:>5} signs fall in the clear and would be read out")
    print(f"  {unnamed['clean_speech']:>5.0f}s of clean solo speech to cut one reference from"
          f"   {verdict(unnamed['clean_speech'])}")

    print(f"\nDub it as a solo read — one actor for the whole episode, the way an "
          f"amateur dub\nhas always been made. See docs/dubbing.md:\n")
    print(f"  python3 scripts/dub_script.py EPISODE.mkv -o utterances.json --solo")


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

    paths = [extract_subtitles(source) for source in args.sources] if args.mkv else args.sources
    stats, unnamed = survey(paths)

    if not stats:
        report_unnamed(unnamed, len(paths))
        return 0

    print(f"{'CHARACTER':<18}{'lines':>6}{'speech':>9}{'clean':>7}{'usable':>9}  verdict")
    print("-" * 80)
    for speaker, entry in sorted(stats.items(), key=lambda item: -item[1]["clean_speech"]):
        print(f"{speaker:<18}{entry['lines']:>6}{entry['speech']:>8.0f}s"
              f"{entry['clean']:>7}{entry['clean_speech']:>8.0f}s  {verdict(entry['clean_speech'])}")

    cloneable = sum(1 for entry in stats.values() if entry["clean_speech"] >= USABLE)
    total_lines = sum(entry["lines"] for entry in stats.values()) + unnamed["lines"]
    dubbed_speech = sum(entry["speech"] for entry in stats.values())
    print(f"\n{total_lines} dialogue lines, {dubbed_speech/60:.0f} min of speech, "
          f"{len(stats)} speakers, {cloneable} with enough audio to clone")
    if unnamed["labelled_files"] < len(paths):
        print(f"{len(paths) - unnamed['labelled_files']} of the surveyed files name "
              f"nobody at all; their {unnamed['lines']} lines are not in the table above")


if __name__ == "__main__":
    sys.exit(main())
