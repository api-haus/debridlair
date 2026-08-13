---
name: dub-label
description: Fill in a solo dub's speaker worksheet for one episode — read the dialogue and say who is probably speaking each line, leaving blank whatever would be a guess. Use when labelling an episode for scripts/dub_label.py, or when asked who says what in a subtitle track that names nobody.
---

# /dub-label — say who is speaking, where the writing says so

`scripts/dub_label.py` writes a worksheet of one episode's lines. Your job is
to fill in the `role` field on the lines whose speaker the writing makes plain,
and to leave it empty everywhere else.

Read `docs/dubbing.md` first if you have not. The one thing to understand
before starting: **this is a solo dub.** One actor reads every line. A role
does not choose a voice — there is only one voice — it moves the read by a
semitone or two. So a label you get wrong costs a slightly odd colour on one
line, and a label you leave blank costs nothing at all, because an unlabelled
line reads in the actor's own register, which is the sound the whole dub is
built around.

That is the trade. Blank is a correct answer. A full worksheet is a wrong one.

## Do it in one pass

```bash
python3 scripts/dub_label.py dub/<show>/work/s01e01.utterances.json   # writes the worksheet
```

The worksheet is JSON, one entry per line, in the order the episode plays:

```json
 "17": {"at": "1:24.1", "kind": "speech", "role": "", "text": "K-Kamotake?"},
 "18": {"at": "1:25.6", "kind": "speech", "role": "", "text": "Kamotaketsunomikoto."},
```

Read it start to finish before answering anything. Attribution comes from the
conversation running through the lines around one — who was addressed, who
asked the question being answered, who was named a moment ago — and none of
that is visible line by line.

**Do not edit the worksheet.** Answer beside it, in a `.labels.txt` next to
it, one line per role, written in a single pass:

```
17 OZU
18 AKASHI
40 -  skip
```

Id, then the role. A line you are not answering is simply left out. A third
column sets `kind` in the two cases below, with `-` in place of a role where
there is none.

## What goes in a role

- **A character's name, as the episode says it.** `OZU`, not `Ozu's friend`.
  Upper case. The same spelling every time, and the same spelling across every
  episode of the show — a role is a colour that should hold from episode one
  to episode eleven.
- **`NARRATION`** for a line that is voice-over, an on-screen explanation, or a
  caption read to the audience rather than said to another character. On a
  narrated show this is most of the episode, and that is fine and expected.
  It reads in the actor's own register, uncoloured — narration is not a voice
  the actor is doing, it is the actor, and it is the neutral the characters
  are heard as departures from.
- **Empty** for everything else. Crowd noise, a walk-on with two lines, a
  scene where two people talk and the writing never says which is which.

`NARRATION` and empty therefore render identically. Prefer `NARRATION` where
you are sure the line is addressed to the audience and empty where you simply
do not know, so the drop report says which of the two you meant.

## What gets a kind

Leave the third column off unless one of these is true:

- **`skip`** — the line is not speech and should not be read aloud at all: a
  translator's note explaining a pun, a sign already read a moment ago, a
  subtitle credit. The dub drops it.
- **`sign`** — the line is writing on the screen rather than something said: a
  letter, a shop front, a caption the parser filed as dialogue. It gets read
  in a flatter register.

Never change a `sign` to `speech` to make it read better. The parser marked it
from where the fansubber put it on the picture, which is stronger evidence
than the text reads like.

## Then merge it

```bash
python3 scripts/dub_label.py dub/<show>/work/s01e01.utterances.json --apply
```

This is where your work is checked, and it will throw parts of it away:

- a role whose name is never spoken aloud anywhere in the episode
- a role carried by fewer than three lines
- everything past the tenth role
- the whole labelling, if you filled in nearly every line or nearly none

Read what it dropped. A long drop list is the tool telling you that you were
attributing rather than reading — go back to the worksheet rather than
reaching for `--allow-unnamed`. That flag exists for a show whose characters
genuinely are never addressed by name, not for a cast list you were confident
about.

## Dispatching this over a season

One agent per episode, each on the episode's own worksheet, run on Sonnet —
this is a reading task with a cheap failure mode and it does not need a larger
model. Do not hand one agent a whole season: the worksheets are long, the
attribution depends on holding a conversation in mind, and a context filled
with eleven episodes is how a cast list starts getting filled in from memory
instead of from the page.

Roles must be spelled the same way across the season, so tell each agent the
spellings the earlier episodes settled on.
