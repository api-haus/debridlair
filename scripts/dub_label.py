#!/usr/bin/env python3
"""Collect a guess at who says each line, and refuse the parts of it that guess.

A solo read does not need to know who is speaking. It sounds like an amateur
dub because one actor reads everything, and that is the point rather than a
compromise. But an actor doing that does shade the read from character to
character, and a subtitle track that names nobody gives nothing to shade on.

A language model can read the dialogue and say who is probably speaking. It is
right often and confidently wrong sometimes, which is exactly the input this
pipeline is built not to trust. So the labels are wired where being wrong is
cheap: a role only ever colours a line by a semitone or two. Nothing here can
put a character in the wrong voice, because there is only one voice.

The checks below are the rest of that answer. They are mechanical on purpose —
a rule that has to be remembered is a rule that gets skipped on episode nine:

  - A rewritten line is dropped, the same staleness guard the adaptations use.
  - A role has to be a name the episode actually says out loud. A model that
    invents a plausible cast list invents plausible names, and a name nobody
    ever speaks is the signature of it.
  - A role carried by one or two lines is dropped. Real characters recur.
  - The cast is capped. Past a certain size a labelling is not attribution any
    more, it is a guess per line.
  - A labelling with a role for nearly every line, or almost none, is refused
    whole. Both shapes mean the model was not reading the dialogue.

    # 1. write the worksheet
    python3 scripts/dub_label.py dub/work/s01e01.utterances.json

    # 2. fill in "role" for the lines whose speaker is plain from the writing
    # 3. merge what survives the checks
    python3 scripts/dub_label.py dub/work/s01e01.utterances.json --apply
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_script import PLAIN_ROLES  # noqa: E402

# A role has to earn its place: this many lines before it colours anything.
MIN_ROLE_LINES = 3

# How many roles a solo read can usefully hold. An actor doing voices has a
# handful of them, and the tail of a longer list is single scenes anyway.
MAX_ROLES = 10

# Labelled shares outside this band are refused whole. Almost nothing means
# the worksheet came back untouched; almost everything means every line was
# assigned to somebody whether or not the writing said so.
MIN_LABELLED, MAX_LABELLED = 0.1, 0.95

# Distinct roles per labelled line, past which this is a guess per line rather
# than a reading of the dialogue.
MAX_ROLE_DENSITY = 0.15

# Shorter than this and a name matches half the dialogue by accident. Three
# rather than four because plenty of characters have three-letter names — Ozu
# is spoken 24 times in the first episode of the show this was built for, and
# a four-letter floor threw him out as invented.
MIN_NAME = 3

KINDS = ("speech", "sign", "skip")


def timestamp(seconds):
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def write_worksheet(utterances, path):
    """One line per utterance, so a line can be edited without touching another."""
    rows = []
    for utterance in utterances:
        rows.append((str(utterance["id"]), {
            "at": timestamp(utterance["start"]),
            "kind": utterance.get("kind", "speech"),
            "role": utterance.get("role") or "",
            "text": utterance["text"]}))

    body = ["{"]
    body += [f' "{key}": {json.dumps(entry, ensure_ascii=False)},' for key, entry in rows]
    body[-1] = body[-1].rstrip(",")
    body.append("}")
    Path(path).write_text("\n".join(body) + "\n", encoding="utf-8")


def read_answers(path):
    """A compact `id role [kind]` list, one line each.

    The worksheet is written to be read, and editing three hundred JSON
    entries in place to answer it is a great deal of work for a file that only
    ever gains one short field per line. Answering alongside it costs one
    write, and loses nothing: the worksheet still carries the text each answer
    is checked against, so a re-parse is still caught.

        17 OZU
        23 NARRATION
        40 -  skip      # no role, and not to be read aloud at all
    """
    answers = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        answers[parts[0]] = {"role": "" if len(parts) < 2 or parts[1] == "-" else parts[1]}
        if len(parts) > 2:
            answers[parts[0]]["kind"] = parts[2]
    return answers


def spoken_names(utterances):
    """Every word the episode says out loud, to check a role against."""
    words = set()
    for utterance in utterances:
        for word in re.split(r"[^A-Za-z]+", utterance["text"].upper()):
            if len(word) >= MIN_NAME:
                words.add(word)
    return words


def is_named(role, spoken):
    """Does the episode ever say this name?

    Loosely, and on purpose: a role written "POLAR BEAR" is named by anyone
    saying "Bear", and "AKASHI" by anyone saying "Akashi-san", because the
    words come apart the same way. What it rejects is a name that appears
    nowhere in the dialogue at all, which is what an invented cast looks like.

    It errs towards accepting, because it should. A wrongly accepted role
    colours some lines that may not be that character's, and a role is worth a
    semitone. A wrongly rejected one throws away a reading that was right.
    """
    if role in PLAIN_ROLES:
        return True
    parts = [word for word in re.split(r"[^A-Za-z]+", role.upper()) if len(word) >= MIN_NAME]
    return any(any(word.startswith(part) or part.startswith(word) for word in spoken)
               for part in parts)


def apply_labels(utterances, worksheet, allow_unnamed=False):
    """Merge the labelling, minus everything that fails a check.

    Returns the utterances and an account of what was thrown away, because the
    account is the useful half: it says whether the model read the episode or
    filled in a form.
    """
    by_id = {str(utterance["id"]): utterance for utterance in utterances}
    dropped = collections.Counter()
    proposed, kinds = {}, {}

    for key, entry in worksheet.items():
        utterance = by_id.get(key)
        if utterance is None:
            dropped["no such line"] += 1
            continue
        # Ids are positions in the utterance list, so a re-parse moves them.
        # The text is what proves this label still belongs to this line.
        if entry.get("text") != utterance["text"]:
            dropped["line has changed since the worksheet"] += 1
            continue
        if entry.get("kind") in KINDS and entry["kind"] != utterance.get("kind"):
            kinds[key] = entry["kind"]
        role = (entry.get("role") or "").strip().upper()
        if role:
            proposed[key] = role

    share = len(proposed) / len(utterances) if utterances else 0.0
    density = len(set(proposed.values())) / len(proposed) if proposed else 0.0
    if proposed and not MIN_LABELLED <= share <= MAX_LABELLED:
        return None, f"{share * 100:.0f}% of lines came back with a role, which is " \
                     f"outside {MIN_LABELLED * 100:.0f}-{MAX_LABELLED * 100:.0f}%"
    if density > MAX_ROLE_DENSITY:
        return None, f"{len(set(proposed.values()))} distinct roles across " \
                     f"{len(proposed)} labelled lines is a guess per line, not " \
                     f"an attribution"

    counts = collections.Counter(proposed.values())
    spoken = spoken_names(utterances)

    kept = set()
    for role, count in counts.most_common():
        if count < MIN_ROLE_LINES:
            dropped[f"{role}: only {count} line(s)"] += count
        elif not allow_unnamed and not is_named(role, spoken):
            dropped[f"{role}: never spoken aloud in the episode"] += count
        elif len(kept) >= MAX_ROLES:
            dropped[f"{role}: past the {MAX_ROLES}-role cap"] += count
        else:
            kept.add(role)

    for utterance in utterances:
        key = str(utterance["id"])
        role = proposed.get(key)
        utterance["role"] = role if role in kept else None
        if key in kinds:
            utterance["kind"] = kinds[key]

    return {"roles": sorted(kept), "labelled": sum(1 for u in utterances if u["role"]),
            "kinds": len(kinds), "dropped": dropped}, None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("utterances")
    parser.add_argument("-o", "--output", help="the worksheet (default beside the "
                                               "utterances)")
    parser.add_argument("--apply", action="store_true",
                        help="merge a filled-in worksheet back into the utterances")
    parser.add_argument("--answers", metavar="TXT",
                        help="a compact `id role [kind]` list to read the roles from, "
                             "instead of editing the worksheet in place "
                             "(default: the .roles.txt beside it, when it exists)")
    parser.add_argument("--allow-unnamed", action="store_true",
                        help="keep roles whose name is never spoken in the episode")
    args = parser.parse_args()

    utterance_path = Path(args.utterances)
    utterances = json.loads(utterance_path.read_text())
    worksheet_path = Path(args.output) if args.output else utterance_path.with_name(
        utterance_path.name.replace(".utterances.json", ".labels.json"))

    if "role" not in utterances[0]:
        raise SystemExit(f"{utterance_path} is a cast list, not a solo read — its "
                         f"speakers come from the subtitle track's own actor field "
                         f"and there is nothing here to add. Re-parse with "
                         f"dub_script.py --solo to build a solo read.")

    if not args.apply:
        write_worksheet(utterances, worksheet_path)
        print(f"wrote {worksheet_path}: {len(utterances)} lines\n")
        print("Read it through, then write the roles beside it as one line each:\n")
        print(f"  {worksheet_path.with_suffix('.txt')}")
        print("      17 OZU\n      23 NARRATION\n      40 -  skip\n")
        print("Answer only where the writing says who is speaking — a name in the "
              "line\nbefore, a reply to a question somebody just asked. Leave a line "
              "out\nwherever a role would be a guess: an unanswered line reads in the "
              "actor's\nown register, which is the right sound for a line nobody can "
              "attribute.\n")
        print(f"Then: python3 scripts/dub_label.py {utterance_path} --apply")
        return 0

    if not worksheet_path.exists():
        raise SystemExit(f"no worksheet at {worksheet_path} — run without --apply first")

    # The worksheet supplies the text every answer is checked against, whether
    # the roles were written into it or answered beside it.
    sheet = json.loads(worksheet_path.read_text())
    answers_path = Path(args.answers) if args.answers else worksheet_path.with_suffix(".txt")
    if answers_path.exists():
        answered, unknown = read_answers(answers_path), 0
        for key, entry in answered.items():
            if key in sheet:
                sheet[key] = {**sheet[key], **entry}
            else:
                unknown += 1
        print(f"read {len(answered)} answers from {answers_path}"
              + (f", {unknown} of them for lines that are not in the worksheet"
                 if unknown else ""))

    result, refused = apply_labels(utterances, sheet, args.allow_unnamed)
    if refused:
        raise SystemExit(f"refusing this labelling whole: {refused}.\nThe utterances "
                         f"are untouched and the read stays in one register, which "
                         f"is\nworse than a good labelling and better than a "
                         f"confident bad one.")

    utterance_path.write_text(json.dumps(utterances, indent=1, ensure_ascii=False))

    print(f"{result['labelled']} of {len(utterances)} lines coloured by "
          f"{len(result['roles'])} roles: {', '.join(result['roles'])}")
    if result["kinds"]:
        print(f"{result['kinds']} lines reclassified between speech, sign and skip")
    if result["dropped"]:
        print("\ndropped:")
        for reason, count in result["dropped"].most_common():
            print(f"  {count:>4} line(s)   {reason}")
    print(f"\nwrote {utterance_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
