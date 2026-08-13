---
name: dub
description: Run, watch, pause and resume a season-long dub. Use for "/dub status", "/dub halt", "/dub resume <show>", "pause the dubbing", "how far along is the dub", or any request to carry on dubbing a show whose episodes are already prepared.
---

# /dub — run a season, and be stoppable while you do

`scripts/dub_season.py` renders a prepared season one episode at a time. This
skill is how you drive it and how you report on it. It is not how a show gets
prepared: parsing, voice banking, adaptation and overdubs come first and live
in `docs/dubbing.md`. If the show has no plan yet, you are in that document,
not this one.

A season is named by its title, and by any title it lists in `aliases`. Naming
nothing means every prepared season, which is right for a status and wrong for
a run.

```bash
python3 scripts/dub_season.py --status              # every season
python3 scripts/dub_season.py "Shirokuma Cafe"      # run it, or carry on
python3 scripts/dub_season.py --halt                # stop whatever is going
```

## /dub status

Run `--status` and relay it. It reads the answer off the disk every time, so
it cannot be stale and you never need to remember what an earlier session did.

Read the three states literally. `done` means the episode is in the library.
`part` means a render stopped part way and names the line it will resume from.
`waiting` means nothing has been drawn — or, if it says `NOT PREPARED`, that
the episode is missing a stem, a video or a parse and the run will skip it.

Quote its numbers rather than estimating. If the user wants a finishing time,
episodes have been running twelve to fifteen minutes each on this box, so
multiply — and say it is an estimate from the observed pace.

## /dub halt

Run `--halt`. That is the whole answer.

**Never kill the process.** `--halt` writes a `PAUSE` file that the render
checks between lines; it finishes the line it is holding, keeps every line it
has drawn, and exits. Killing it loses the line and can leave a half-muxed
episode nothing will clean up. If a render is already past synthesis and into
the mix, `--halt` lets it finish that episode — the GPU is idle by then, and
that is what the stop was for. Report that it did.

**Never delete a `clips/` directory to tidy up.** That directory is the resume
point. A finished episode clears its own.

Confirm what actually stopped, from `--status`, rather than from having asked.

## /dub resume &lt;show&gt;

```bash
python3 scripts/dub_season.py "<show>" >> dub/<work>/season.log 2>&1
```

Run it in the background, then arm a monitor on the log so each episode
reports itself instead of you polling:

```
tail -f -n0 dub/<work>/season.log | grep -E --line-buffered \
  "^=== S01E|lines placed|^paused\.|failed with exit|^Traceback|is fully rendered"
```

Running the tool clears the `PAUSE` file by itself — running it *is* the
instruction to run. Do not delete the file by hand and do not pass a flag to
override it.

Then say what happened per episode as it lands: lines placed, lines still
overrunning, and anything in the drift table marked `<- drifted`. A number of
overruns in the high teens is worth flagging for a listen; drift outside ±10%
on a character with more than a few lines is worth investigating.

### When the title does not resolve

The tool prints every title it knows. If the user's name for the show is a
real alternate title the plan does not list — a Japanese title for a show
prepared under its English one, or whatever they happen to call it — **add it
to `aliases` in that plan** and run again. Do not just resolve it in your head
and move on: the next session will not have your head, and the plan is where
that knowledge belongs.

If it is genuinely a different show, say so; do not start dubbing something
nobody asked for.

## What not to build

There is a tool for this. Do not write a shell loop over `dub_render.py`, do
not keep a progress file, and do not track episode numbers in your own notes.
Every one of those has been tried here and every one went stale — a hand-kept
note told a later session to resume with a command that had stopped being the
right one. Progress is read off the disk or it is not read at all.

## Before any of it

`python3 scripts/dub_check.py` — exit 0 means this box can dub. On a box where
it exits 1 the feature does not exist: say plainly that this machine cannot,
and do not describe it as something to enable later.
