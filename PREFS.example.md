# PREFS — how you like your library

Copy this to `PREFS.md` and edit it. `PREFS.md` is gitignored, so it stays
yours and never collides with an upstream pull.

You do not have to edit it by hand. Tell the agent what you want — *"stop
fetching anything over 20 GB"*, *"dubs are fine for kids' stuff"*, *"never ask
me about subtitle variants again"* — and it will write the change here.

**On conflict, this file wins over `AGENTS.md`.** AGENTS.md describes how the
stack works and must not drift per-user; this file is taste.

## Audio and subtitles

Original audio only. Never a dub-only release — no Latino/Castilian dubs, no
Russian MVO/DVO voice-overs, nothing where the original track is missing.
Dual-audio releases are fine but rank below plain original-audio ones.

English subtitles are the default and stay on.

This is a standing policy, not a per-title question: **do not ask which
subtitle variant I want.** The auto-pick in `torbox_find.py` ranks on
resolution and seeders and does not check subtitle language, so when the
winning release turns out to have no English subs, quietly requeue the best
one that does and drop the other.

## Quality and size

Everything has to stream inside the shared bandwidth cap, so bigger is not
better. No remuxes. Ceilings: about 12 GB an episode, 30 GB a movie, 80 GB a
season pack.

> These ceilings are enforced by `EP_MAX` / `MOVIE_MAX` / `PACK_MAX` at the top
> of `scripts/torbox_find.py`. If you change the numbers here, change them
> there too — that is a local edit to a tracked file, so keep it out of any PR
> you send upstream.

Otherwise: prefer healthy seed counts, and prefer 1080p that plays over 2160p
that buffers.

## Series

A request for a show is a request for the whole show — every season, plus
specials, featurettes and extras where they are indexed. Do not stop at season
one and wait to be asked again. Only fetch a single season when I say so.

## Asking vs. acting

Fetch first, tell me after. Naming a title is the request; I do not want to be
asked "shall I grab this?" — that includes titles the agent itself suggested a
moment earlier. Deleting a superseded release works the same way: delete it,
then say what went and why.

Check the library before queuing, though. A duplicate under a slightly
different release name shows up as a second entry, not a clean replace.

## Notes

Free-form. Anything that does not fit above goes here, in whatever shape suits
it.

**Only write here when I ask you to.** Do not keep a record of what I watch,
do not infer what I like from the library, and do not add a line because you
think you noticed a pattern. Recommendations should come from what I tell you
in the conversation, not from a profile you have been quietly building.
