---
name: hello-debrid
description: Brief a new user on their debridlair preferences and write PREFS.md from their answers. Use on a fresh install, whenever PREFS.md is missing, or when the user asks to set up, review or change their preferences.
---

# /hello-debrid — write PREFS.md with the user

The shipped defaults are one person's taste. Somebody else wants dubs, has a
faster line, or would rather be asked before anything is fetched. This is the
conversation that finds that out and writes it down.

Budget about a minute of the user's attention. A handful of questions with the
common answers offered, then a file. Not an interrogation, and not a form.

## 1. Read the shape from PREFS.example.md

`PREFS.example.md` is the source of truth for which preferences exist. **Read it
first, every time.** Each `##` section is one topic to ask about, and its prose
states the shipped default.

If the file disagrees with the table below, the file wins — it may have grown a
section since this skill was written. Ask about what is in the file, not what is
in here.

Skip the **Notes** section. It is free-form, it stays empty until the user asks
for something to go in it, and its whole point is that nobody fills it in on
their behalf.

## 2. Ask once, with concrete options

One question per section, all in a single call so the user answers them
together — `AskUserQuestion` in Claude Code, a plain numbered list in any agent
without it.

How to write the options:

- The shipped default goes first, labelled `(Recommended)`.
- Describe outcomes the user recognises, not policy. *"Never a dubbed film"*
  beats *"original audio only, dub-only releases refused"*.
- Three options is usually right. The user can always answer in their own words.
- **Never ask about taste** — not what they like, not what they watch, not what
  to recommend. That is not what this file is for.

The mapping as `PREFS.example.md` stands today. Verify it against the file
before you use it:

| Section | Header | Question | Options |
|---|---|---|---|
| Audio and subtitles | Audio | How should foreign-language films and shows arrive? | Original audio with English subtitles *(Recommended)* — never a dubbed release · Dubbed when a dub exists, subtitles off · Original audio, subtitles only when it isn't in English |
| Quality and size | Bandwidth | How much bandwidth does this box have for streaming? | Modest, around 40 Mbit *(Recommended)* — caps at 12 GB an episode, 30 GB a film, 80 GB a pack, no remuxes · Fast, 100 Mbit or better — roughly double those · Gigabit or local — no ceiling, remuxes allowed |
| Series | Series | When you ask for a show, what should arrive? | The whole show, specials included *(Recommended)* · Only the season you name · Ask each time |
| Asking vs acting | Autonomy | How much should it check with you before fetching? | Fetch first, tell you after *(Recommended)* · Confirm before every fetch · Confirm only when something is replaced or deleted |

## 3. Write PREFS.md

Copy `PREFS.example.md` and rewrite each section to match the answers. Keep its
structure, its headings and its voice — prose a person can read and edit, not a
config dump. A section the user took the default on keeps the example's wording.

Two things carry over verbatim: the note that this file **overrides
`AGENTS.md`**, and the **Notes** section with its instruction never to record
what the user watches or infer their taste. Those are not preferences.

## 4. Sync the ceilings if they moved

If the user chose anything other than the default ceilings, edit `EP_MAX`,
`MOVIE_MAX` and `PACK_MAX` at the top of `scripts/torbox_find.py` to match, and
say you did. `PREFS.md` states the policy; that script is what enforces it, and
a `PREFS.md` the finder disagrees with is worse than no `PREFS.md` at all.

For *"no ceiling, remuxes allowed"*, raise the three constants well past any
real release size rather than deleting the check, and leave the remux rule in
place unless the user is explicit — remuxes are the one thing that reliably
cannot stream.

## If PREFS.md already exists

Never clobber it. Read it, say in a sentence per section what it currently sets,
and ask which of them to revisit. Then edit those sections in place and leave the
rest alone.

## Close

Confirm in a couple of lines what was written, and tell the user the file is not
something they have to maintain: saying *"stop fetching anything over 20 GB"* or
*"dubs are fine for kids' films"* in an ordinary session is enough, and the
agent will edit it for them.

Then get on with whatever they actually came for. If they opened the session
asking for a film, fetch the film.
