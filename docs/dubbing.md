# Dubbing a subbed-only show

Some shows were never dubbed. This pipeline makes an English track for one,
locally, keeping each character's own voice: it clones every speaker from their
original performance and lays the result over the untouched music and effects.

Nothing here touches `library/`. That directory is owned by `torbox_sync.py`
and pruned to match Torbox every 15 minutes, so a locally rendered file left
there is deleted on the next pass.

Finished episodes go to `dub/finished/tv/<Show>/Season NN/`, which is mounted
at `/media/dub/tv` as its own Emby library, "TV Shows (Dub)". Name them the
way the shows already there are named:

```
dub/finished/tv/The Tatami Galaxy/Season 01/S01E01 - The Tatami Galaxy - 01 [Dub].mkv
```

Everything before that — the fetched video, the stems, the scripts, the
previews — is working state under `dub/<show>/`, and all of `dub/` is
gitignored.

## What makes a machine able to dub

Before anything else, and before telling a user this feature exists:

```bash
python3 scripts/dub_check.py --full
```

A CUDA GPU with 8 GB of VRAM, 40 GB free, and an ffmpeg carrying the
rubberband filter. Below that the run does not merely take longer, it fails or
thrashes, so the honest answer on such a box is that dubbing is not available
here — not that it could be arranged.

## What makes a show dubbable

The pipeline does not transcribe and does not translate. It reads the English
subtitle track that shipped with the release, so a show is dubbable exactly
when its subtitles are good enough to speak.

The deciding factor is the ASS *actor* field. A fansub that labels each line
with its character gives per-line speaker attribution for free, which is the
part automatic dubbing normally gets wrong. Check before spending GPU time:

```bash
python3 scripts/dub_survey.py --mkv "dub/source/s01e01.mkv"
python3 scripts/dub_survey.py season01/*.ass        # pool a season
```

Read the verdict column. A show whose main cast reaches `usable clone` or
better gets the dub this pipeline was built for: every character cloned from
their own performance, which is the rest of this page.

A show whose track names nobody gets a different dub rather than no dub. The
survey says which of the two you are looking at and stops — see [dubbing a
track that names nobody](#dubbing-a-track-that-names-nobody).

`dub_survey.py --mkv` falls back to an untagged text subtitle track when
nothing is tagged `eng`/`en` — plenty of fansub releases never set the
language tag at all, and a release shipping one track with karaoke typesetting
and one without gets the one without. An untagged track is not the same
problem as an unlabelled one and the tool tells the two apart, because they
have different answers.

### Checking a release before it's in the library

The release already synced in is not always the one with labelled subs, and a
niche show is often re-encoded by several groups sharing the same unlabelled
script. Don't sync a candidate just to find out:

```bash
python3 scripts/torbox_add.py "magnet:?xt=..."       # queue it, note the torrent_id
python3 scripts/torbox_peek.py TORRENT_ID --grep S01E01   # direct URL, no local download
python3 scripts/dub_survey.py --mkv "<url from above>"
```

`torbox_peek.py` prints each file's direct-download URL straight from Torbox
— the same kind a `.strm` points at — so `dub_survey.py` (and `ffprobe`/
`ffmpeg` generally) can read just the subtitle track over HTTP without
pulling the video to disk. Delete the ones that turn out unlabelled (the
Torbox-deletes rule in `AGENTS.md`); only sync the one that's worth dubbing.

## Running it

Each stage writes files the next stage reads, so a run can be stopped and
resumed. Stages are separate tools because they fail for different reasons and
cost different amounts to repeat.

This is the cast dub, for a track that names its speakers. The solo read runs
through the same four stages with `--solo` and differs at each one, so it is
written out separately below rather than as exceptions to this.

### 1. Prepare the episodes

Fetches each episode to local disk, splits the audio into a vocals stem and a
music-and-effects bed, and parses the subtitles into utterances.

```bash
python3 scripts/dub_prepare.py "library/tv/Polar Bear Cafe/Season 01" --limit 4
```

Stem splitting runs on the GPU through the `gpu` process queue, so parallel
agent sessions do not oversubscribe the card. Re-running skips finished work.

**One show per working directory.** Episodes are named by season and episode
number, so a second show prepared into the same `--work` overwrites the first
show's stems, scripts and rewrites — `s01e01` is `s01e01`. Give each show its
own: `--work dub/tatami`. The virtualenv is not part of that and stays at
`dub/.venv` for every show, since it is a tool rather than one show's state.

### 2. Mint the voice bank

Cuts one reference clip per character from the vocals stem and reports what it
found. Pass several episodes to pool a supporting cast across a season.

```bash
dub/.venv/bin/python scripts/dub_voices.py -o dub/voices/ \
    --episode dub/work/s01e01.utterances.json dub/stems/htdemucs/s01e01.audio \
    --episode dub/work/s01e02.utterances.json dub/stems/htdemucs/s01e02.audio
```

**Read the two right-hand columns before going on.** `clean` is the ratio of
voice to music in the chosen clips; `pitch` is the median voiced pitch of the
finished reference. They are there to catch contaminated references, and they
work: on the first run of Polar Bear Cafe, Panda's mother came back at 135 Hz —
a baritone — because her clips were scored at 4x and the pitch tracker was
following the music rather than her. Raising the cleanliness floor dropped her
from the bank instead of cloning a wrong voice from polluted audio.

A character the bank drops is not lost. Pool more episodes; a minor character
who has three clean seconds per episode becomes clonable across a season.

Clips are chosen by scanning within each line rather than taking the line
whole. A line only half covered by music still holds a second of the character
in the clear, and taking it is often the difference between a supporting
character being banked and being dropped.

### When a character has no clean audio anywhere

Some never do: a tannoy announcement carries processing and room noise, a
one-line walk-on has nothing to spare. Those are written to
`voices/understudies.json`, cast to the banked voice nearest in pitch, so their
lines are still dubbed. A single line left in the original language in the
middle of a dubbed scene is more jarring than an approximate voice.

**Check that file, and edit it.** The casting reads pitch from the same
music-heavy audio that failed the cloning bar, so it is a guess and it is
sometimes plainly wrong — on Polar Bear Cafe it put Panda's mother at 182 Hz
and cast her as a male llama. It is plain JSON keyed by character; point the
`voice` field at any banked name and re-render. `--no-understudies` turns the
whole mechanism off and leaves those characters in the original language.

### 3. Rewrite the lines that cannot be spoken in time

Some lines cannot be delivered however the timing is arranged. A subtitle is
written to be read in the time it is on screen; spoken, the same words often
take longer than the shot allows, and English out of Japanese is usually the
longer of the two. Studios have always answered this by rewriting rather than
translating — the trade calls it adaptation.

```bash
python3 scripts/dub_adapt.py dub/work/s01e01.utterances.json \
    --timing dub/preview/s01e01.dubbed.mkv.timing.json
```

The threshold is measured rather than assumed. A previous render reports how
long the model took over each line, which gives the rate it actually speaks
at, and anything demanding more than that rate times the compression ceiling
is undeliverable. On Polar Bear Cafe that is 2.33 words per second against a
3.15 ceiling, flagging 37 lines of 343.

Fill in `adapted` for each line in the file it writes, keeping the meaning and
the register — it ships the neighbouring lines as context and a word budget —
then re-render. `--review` reports what each rewrite bought and flags any that
are still too long. `--no-adaptations` speaks the subtitle text as written.

Rewrites are carried across a re-parse by speaker and text, never by id: an id
is a position in the utterance list, so re-parsing renumbers them. Carrying
them by id once put 15 of 37 rewrites on a different character's line, which
renders perfectly and is silently wrong. `dub_render` refuses any rewrite
whose stored original no longer matches its line, and says so.

### 4. Render

Speaks each line in its character's cloned voice and mixes it over the bed.
Point `--from` / `--to` at a scene to preview before committing to an episode.

```bash
processqueue gpu dub/.venv/bin/python scripts/dub_render.py \
    dub/work/s01e01.utterances.json dub/voices/ dub/stems/htdemucs/s01e01.audio \
    --video dub/source/s01e01.mkv --from 20:38 --to 21:52 -o dub/preview/cafe.mkv
```

Render through the `gpu` queue. Inference holds most of the card for the whole
run, so two renders at once do not run slower, they fail: the second dies with
a CUDA out-of-memory part way through a scene it has already half generated.

The output keeps the Japanese track and adds the dub beside it, so nothing is
replaced and Emby simply shows an extra audio track. The music and effects bed
stays stereo; the voices are mono and, almost always, sit in the centre where
screen dialogue belongs — the rare exception is a line inside a resolved
overdub case (below), which gets a stereo position instead.

Every run ends with a drift table comparing each character's pitch in the
**finished dub track** against their reference. That it reads the finished
track rather than the clips coming out of the model is the point, and it was
learned the hard way: an early version measured the clips, a resampling fault
downstream dropped every time-compressed line an octave and doubled its
length, and the check reported all clear on a track where most lines were
ruined. A verification that does not look at the artifact you ship is not a
verification.

A character marked `<- drifted` lost its voice somewhere between the model and
the mix. Re-mint its reference from cleaner clips or pool more episodes, and if
the reference is clean, suspect the fitting stage rather than the clone.

`scripts/dub_script.py --scenes N` ranks the best scenes to preview, by how
many characters speak in them. A preview with one voice in it proves nothing.

### 5. Render the rest of the season

One episode is a quarter hour of GPU, so a season is most of a working day of
it, and nobody sits through that in one go. `scripts/dub_season.py` runs the
episodes one at a time, stops whenever it is asked to, and resumes from where
it stopped — including from a different session days later.

It reads one plan file, and that file is the whole memory of the run:

```json
{
  "show": "Polar Bear Cafe",
  "aliases": ["Shirokuma Cafe", "panda"],
  "season": 1,
  "work": "dub/work",
  "voices": "dub/voices_test",
  "stems": "dub/stems/htdemucs",
  "source": "dub/source",
  "library": "dub/finished/tv",
  "python": "dub/.venv/bin/python",
  "queue": "gpu",
  "options": []
}
```

Every key but `show` has the default shown above, and `options` goes to
`dub_render.py` verbatim — which is where a season-wide `--solo` belongs.
Plans are found at `dub/*/season.json`, and paths inside one are relative to
the repository rather than to wherever the command was run.

A season is named by its title or by any of its `aliases`, so a show asked for
under a title it also goes by resolves without anyone knowing where its files
live. Naming nothing means every prepared season — what you want from a status
and not from a run.

```bash
python3 scripts/dub_season.py --status                     # every season
python3 scripts/dub_season.py "Shirokuma Cafe" --status    # where one stands
python3 scripts/dub_season.py "Shirokuma Cafe"             # run, or carry on
python3 scripts/dub_season.py "Shirokuma Cafe" --limit 1   # just the next one
python3 scripts/dub_season.py --halt                       # stop whatever is going
```

When a show is asked for under a title its plan does not list, put the title
in `aliases` rather than resolving it once and moving on. The next session
resolving it is the point.

**Nothing about the run is written down.** Where it got to is read off the
disk every time: an episode is done because the episode is in the library, and
a line is drawn because its clip is in `dub/work/clips/`. A progress note can
be wrong; a file cannot. This is why a session that knows nothing about an
earlier one can pick the season up correctly.

**Stopping is a file.** `--halt` writes `dub/work/PAUSE`, and the render checks
for it between lines. It is a file rather than a signal because the session
asking for the stop is often not the session that started the run and has no
process to signal — and because the render sits behind `processqueue`, so a
forwarded signal reaches the queue wrapper rather than the python holding the
model. Running `dub_season.py` again clears the file: running it *is* the
instruction to run, so start and resume are the same command.

**A stop costs one line.** Each drawn line is written to
`dub/work/clips/<episode>/` with a stamp of the text and voice it came from,
and a resumed render reuses every clip whose stamp still matches. So the price
of stopping is the line in the model's hands, not the quarter hour behind it.
The stamp is what makes reuse safe: change an adaptation, or re-mint the voice
bank, and the affected lines redraw instead of quietly keeping the old take.
The clips are cleared once the episode is finished — `--keep-clips` on
`dub_render.py` keeps them.

**A killed render never leaves a broken episode.** The mux writes to a hidden
`.partial` file beside the finished name and moves it into place only when
ffmpeg has returned, so Emby cannot index a truncated mkv. The timing report
lands first, so the episode and its report always appear together.

Asked to stop while an episode is already mixing, the render finishes it. That
part is CPU and minutes; the GPU is already idle, which is what the stop was
for. It then exits without starting another.

## Giving overlapping dialogue a stereo position

Rare, and worth its own pass rather than a fixed rule in `dub_render.py`: two
characters' own lines occasionally collide in time — not a unison line, which
already says who speaks it together, but simply two people talking over each
other. Summed to the centre, both dubbed voices land on top of one another and
the scene turns to noise, which is what "loud and unreadable" actually sounds
like. `dub_script.py` already spots these while it parses the subtitles; the
rest of this section is what to do once it has.

### Find the cases

Every utterance gets an `"overdub"` field: `null`, or the id of the case it
belongs to, whenever its span genuinely overlaps a *different* speaker's line.
A unison line, and anything spoken by a named group, is excluded — that
mechanism already handles who speaks it. Overlap is transitive: if A overlaps
B and B overlaps C, all three are one case even where A and C never directly
touch, because all three still collide in the mixed-down render.

```bash
python3 scripts/dub_script.py dub/source/s01e01.mkv -o dub/work/s01e01.utterances.json --overdubs
```

### Look at what the original mix actually did

```bash
dub/.venv/bin/python scripts/dub_overdub.py dub/work/s01e01.utterances.json \
    dub/stems/htdemucs/s01e01.audio --all
```

For each case this draws a stereo-panning spectrogram of the separated vocals
stem across the case's own span: colour says which side of the stereo field a
moment of sound sits on, opacity says how loud it is, so silence reads as
background rather than as a confident claim about where nothing is. Below it,
a timeline strip shows who is speaking when, and a dot plot shows each
character's own *solo* pan — measured from their nearest lines outside any
case — as a starting estimate of where the original placed them.

That estimate is only as good as what the original mix actually did, and it
is not always anything. On Polar Bear Cafe the vocals stem carries no real
stereo image anywhere that was checked, not only inside the flagged cases —
left and right agree to a fraction of a decibel throughout an episode. The
tool reports that honestly (`solo pan +0.00`) instead of inventing a position,
and the picture backs up the number rather than just repeating it: a
spectrogram that reads flat, neutral grey everywhere is the same finding drawn
out. When that happens, matching the original mix is not an available goal;
telling the colliding voices apart still is, and that is what resolving the
case below is actually for.

### Resolve the case

The analysis writes `<slug>.overdubs.json`, one entry per case, holding the
measured `solo` stats, a naive `proposed` position taken straight from them,
and a `resolved` block left `null` until someone — a person listening, or an
LLM reading the image and the numbers — decides. `dub_render.py` only ever
reads `resolved`; `proposed` is scratch space for that decision, not something
it renders on its own. Edit the file directly:

```json
"1": {
 "resolved": {
  "PANDA": {"pan": -0.6, "gain_db": 0.0},
  "PENGUIN": {"pan": 0.6, "gain_db": 0.0}
 },
 "status": "resolved",
 "notes": "why, in one line"
}
```

`pan` runs -1 (hard left) to +1 (hard right); `gain_db` is an extra trim on
top of the usual per-line, scene-matched level, for the rare case where the
original balance between the two needs correcting rather than just
separating. Leave a case at `"status": "proposed"` and it renders centred,
same as before this pipeline existed — `dub_render.py` prints every case it
finds still unresolved, on every run, so one does not quietly stay centred
forever.

Re-running `dub_overdub.py` on a case updates its measurement and its image
without touching a `resolved` block that is already there, the same way
`voices/understudies.json` survives a re-render.

### Render it

Nothing extra to do. `dub_render.py` finds `<slug>.overdubs.json` beside the
utterances by default and applies every resolved case's pan and gain
automatically. A line with no resolved case behaves exactly as it always has:
full gain, dead centre. `--no-overdubs` renders everything centred regardless,
for comparison.

## Dubbing a track that names nobody

Plenty of shows were subbed by someone who never filled in the actor field,
and on those there is no cast to build. The survey says so rather than
printing an empty table:

```
no character is named on any line of 1 file(s) — there is no cast to clone

    404 dialogue lines
   16.1 min of speech
      0 lines overlap another and would be read in sequence
      1 signs fall in the clear and would be read out
    883s of clean solo speech to cut one reference from   strong clone
```

The answer is not diarization. Diarization on overlapping anime dialogue is
wrong often and confidently, and a wrong answer there puts a character in
somebody else's voice for a scene. The answer is the dub amateur studios have
always made and plenty of people grew up on: **one actor reads the whole
episode**, shading the read a little from character to character. It is a
recognised form rather than a degraded one, and it fails gracefully — the
worst a mistake can do is colour a line oddly, because there is only ever one
voice to be in.

Everything below is the same pipeline with `--solo` through it.

### 1. Prepare, parsed for one reader

```bash
python3 scripts/dub_prepare.py "library/tv/The Tatami Galaxy/Season 01" \
    --limit 1 --solo --work dub/tatami
```

Every line goes to one bank entry called `NARRATOR`. Whoever the line belongs
to is kept beside it as a *role*, which is empty until something fills it in.

The parser also classifies each subtitle event, which it has to do without
help from the style names on a release that calls every style `Default`:

- **speech** — left where the player puts it, so it is being said.
- **sign** — positioned on the picture with `\pos` or `\move`, so it is
  writing being translated. Only placement counts. A raised `\an8` alone does
  not, because a fansubber lifts a line to the top of the frame whenever two
  people talk at once, and that is dialogue.
- **drawing** — an ASS `\p1` block, which is vector commands. Read out, it
  says its coordinates.
- **song** — a karaoke or lyrics style. Never dubbed, in either mode.

On The Tatami Galaxy's release that separates 453 events into 404 speech, 27
signs and 22 drawings, with nothing to go on but the override tags.

### 2. Mint the one voice

```bash
dub/.venv/bin/python scripts/dub_voices.py -o dub/tatami/voices/ --solo \
    --episode dub/tatami/work/s01e01.utterances.json \
             dub/tatami/stems/htdemucs/s01e01.audio
```

Nothing here knows who is speaking, so the clean clips are a mix of the whole
cast and joining them would clone an average of everybody. What separates them
without attribution is pitch: the clips are clustered, and the band holding
the most speech wins.

That band is the voice on screen most, which on a narrated show is the
narrator and otherwise is the lead — either way the voice the show already
sounds like, which is the right one to hand a solo dub. On Tatami it reports:

```
clustered 90 of the cleanest clips by pitch: 113 Hz +-2 semitones holds
28 of them, 40% of that audio
```

**Listen to `voices/NARRATOR.wav` before rendering an episode.** Every line is
read in it. If the pick is wrong, `--solo-pitch 190` nominates another band and
`--reference clip.wav` skips the show entirely and uses a voice you supply.

#### Keeping a reader rather than re-cutting one

A voice worth using again is worth naming. `--actor` records who a bank stands
for, and a bank is a directory, so a reader is just one of these:

```bash
dub/.venv/bin/python scripts/dub_voices.py -o dub/actors/anton_vey/ --solo \
    --actor "Anton Vey" --from-role NARRATION \
    --episode dub/tatami/work/s01e01.utterances.json \
             dub/tatami/stems/htdemucs/s01e01.audio

scripts/dub_voices.py --troupe dub/actors/     # what is on the shelf
```

Casting one is pointing the render at its directory, and the run says who read
it. `--reference` stands a reader up from any clean clip without loading a
show at all, which is how one gets a voice type the show itself does not have.
`--from-role` cuts from the lines a labelling gave to one character, with the
pitch clustering behind it throwing out whatever came back a different voice.

A reader can also carry a `tuning.json` beside its bank, which is what makes
one a persona rather than a clip: `{"NARRATOR": {"pitch": -3.0, "pace": 0.93}}`
reads lower and slower than the clip it was cut from, every episode, without
touching the show's own shades.

### 3. Label the roles — optionally, and never trusted far

The shading needs no labels. Each line leans by what the *original* voice was
doing in that exact span: the vocals stem is right there, so the pitch the
character was actually performed at is a measurement, and the read leans that
way — up for the girl, down for the big man — without anything having to name
them first. Against the episode's own neutral, so it travels between actors.

Three things keep it steady. It is shrunk through a curve, so the actor
suggests the difference rather than chasing it and a fifteen-semitone gap does
not peg every line at the limit; it snaps to a quarter tone, so two lines of
one character land on the same shade instead of wandering; and it is clamped
to a couple of semitones, so nothing becomes an impression.

Labels are worth having anyway — they steady a line too short or too buried to
measure, which borrows its role's median instead of falling flat, and they
give the report something to be read against:

```
read by Anton Vey
  the episode's own voices sit around 113 Hz; 310 of its 334 lines
  were measurable and 173 of those lean far enough to hear
  by role, where the labelling named one:
    AKASHI                    289 Hz   +1.50 st
    OZU                       253 Hz   +1.50 st
    KAMOTAKETSUNUMINOKAMI     126 Hz   +0.25 st
    NARRATION                 112 Hz   +0.00 st
```

That table is the thing to check. Narration at 0.00 says the neutral landed on
the voice the reference was cut from; Akashi at +1.50 says the actor lifts for
her. An earlier version took the shade from a hash of the role name, which put
the direction at random, and the audible result was the actor pitching *down*
to play the only girl in the episode.

```bash
python3 scripts/dub_label.py dub/tatami/work/s01e01.utterances.json
# fill in "role" — see .agents/skills/dub-label/SKILL.md, or run /dub-label
python3 scripts/dub_label.py dub/tatami/work/s01e01.utterances.json --apply
```

The worksheet is meant to be filled in by a language model reading the
dialogue, which is a job it does well and sometimes does confidently wrong.
Two things keep that cheap. The first is structural: **a role never picks a
voice**, it moves the read by a semitone or two, so being wrong costs a colour
rather than a character. The second is that `--apply` throws out the parts of
a labelling that look like guessing, and says what it threw:

- a role whose name is never spoken aloud anywhere in the episode — the
  signature of an invented cast list
- a role carried by fewer than three lines
- everything past the tenth role
- the whole labelling, if nearly every line came back with a role or nearly
  none, or if there are more than 0.15 distinct roles per labelled line

A labelling this refuses leaves the utterances untouched and the read in one
register, which is worse than a good labelling and better than a confident bad
one. Read the drop list rather than reaching for `--allow-unnamed`.

Pace and level still come from a hash of the role name, which is only there so
two roles differ and one role holds across a season without anyone writing it
down. `NARRATION` takes no colour at all: narration is not a voice the actor is
doing, it is the actor, and colouring it would leave the episode with no
neutral to hear the coloured lines against. Where a shade is plainly wrong,
`voices/shades.json` overrides it by name:

```json
{"OZU": {"pitch": 1.4, "pace": 1.05, "gain": 1.0}}
```

`pitch` is semitones, `pace` a speed multiplier, `gain` a level trim. Keep
them small. The conceit is one person doing voices, and a wide swing stops
reading as that person acting and starts reading as a second actor spliced in.

### 4. Render it over the original

```bash
processqueue gpu dub/.venv/bin/python scripts/dub_render.py \
    dub/tatami/work/s01e01.utterances.json dub/tatami/voices/ \
    dub/tatami/stems/htdemucs/s01e01.audio \
    --video dub/tatami/source/s01e01.mkv --from 0:45 --to 2:00 \
    -o dub/tatami/preview/open.mkv
```

A solo read says so in the utterance list and the renderer switches defaults
on its own. Two of them matter.

**The original is ducked, not replaced.** A cast dub swaps separated voices
for cloned ones; a voice-over is laid on top of a mix that keeps playing. The
original performances, the score and the effects stay and simply step back
11 dB while the reader speaks. That also disposes of the separation problem
rather than managing it: the two stems are summed back instead of one being
used alone, so whatever Demucs smeared one way is cancelled by the
complementary smear the other, and no artefact reaches the output. And it is
the sound the form actually has — hearing the original actor under the reader
is most of what makes a one-voice dub legible. `--duck 14` leans on it harder,
`--mix replace` renders it the other way.

**One actor never talks over themselves.** Two lines that collide are queued
rather than stacked, because stacking one voice on itself does not make two
voices, it makes one unintelligible one. A queued line comes in behind and
then speaks into whatever room is left, so the lag works itself out instead of
pushing into the rest of the scene. Past 2.5 s it gives up and overlaps: a
moment of doubled voice is a smaller error than answering a shot that has
gone. Every line also comes in 0.2 s behind its subtitle, which is what a
voice-over does so both onsets are audible.

The run reports the lag it accumulated:

```
10 lines waited for the previous one to finish, the latest by 1.5s
```

**The words travel with the dub.** Two subtitle tracks go into the output,
because they answer different questions. *English (the dub's source)* is the
track the dub was built from, copied in its own format so a fansub's
typesetting arrives as the fansub drew it — that one says what the scene
means. *English (as spoken)* is what the reader actually said and when,
carrying the rewrites and the queueing — that one is for the moment a
generated line comes out wrong and you want to know what it was going for. On
the same line they sit 0.2 s apart, which is the voice-over lag.

`--subtitles PATH` names the source track explicitly, which is what a dub
built from a script cut for another release needs; otherwise the video's own
track is used. Both are shifted to the exported span rather than seeked to it:
ffmpeg's `-ss` lands on the nearest cue boundary rather than the time asked
for, which put the source ten seconds out and left 286 stale cues stacked at
zero.

### What this is worse at

Fast shows. The reader has to fit one mouth's worth of English into a scene
written for several, and dense narration overruns: on Tatami's opening 11 of
24 lines overran after fitting, and the queue was running 1.5 s behind.

`dub_adapt.py` is the answer and it is the same tool as for a cast dub. It
flagged 42 of the episode's 334 lines as undeliverable at any timing, and
rewriting those took the same scene to 2 lines overrunning and 0.5 s of lag.
Do it before judging a solo read of a talkative show — unadapted, the reader
sounds rushed everywhere, which is easy to mistake for the voice being wrong.

## When the subtitles come from another release

The release with the labelled fansub is often not the release worth dubbing,
and two encodes of one episode are rarely timed alike. Fed to the dub as they
are, every line is spoken over the wrong shot and nothing downstream notices.

```bash
python3 scripts/dub_align.py --subs labelled.ass --against dub/source/s01e01.mkv \
    -o dub/subs/s01e01.ass
python3 scripts/dub_prepare.py "library/tv/Show/Season 01" --subs dub/subs/
```

Two methods, and the tool says which one answered:

- **By text**, when both sides are subtitles. Several groups routinely ship
  the same translation, so the same sentence is found on both sides and the
  gap read off directly. Exact, and it reports the scatter, so a good answer
  is visibly good — on a test shift of +3.720 s it recovers −3.720 s with zero
  scatter.
- **By activity**, otherwise. Where the text differs, or the target carries no
  subtitle track at all, speech and silence still fall in the same places and
  cross-correlating the two says where. Looser, and it says so in the peak it
  reports: against another subtitle track it recovered a +3.720 s shift to
  within a frame, and against a Demucs vocals stem a separate +5.000 s shift
  came back as 4.750 s, because a subtitle is cued a little before the line it
  belongs to. Point it at a vocals stem rather than a full mix where you have
  one — it cannot tell speech from score, and on a busy soundtrack a full mix
  describes the score.

Then it checks the constant is constant. A script pulled from a PAL transfer
runs 4% fast against a film-rate encode, and no single offset fits it — the
per-line offsets sit on a *line* rather than scattering, which is a different
finding and gets said as one:

```
these are the same script at a different rate, not a different script: the
offsets sit on a line to within 0.002s, sliding -54.2s across 22 minutes —
a rate ratio of 1.0427.
That is PAL 25 fps against film 23.976.
```

It refuses to write a shifted file on a measurement it does not believe,
because a wrong offset is invisible until you watch the episode. `--offset`
sets one by hand and `--force` overrides the refusal.

Editions are a separate matter and this cannot detect them. A director's cut
against a theatrical is not one offset plus noise, it is scenes that exist on
one side only, and the text anchors will scatter without sitting on a line.
Television does not generally have this problem.

## What the pipeline decides for you

- **Signage is told apart by where it sits, not only by what its style is
  called.** Fansub styles separate dialogue from typeset graphics, and where
  they do that settles it. Where every style is called `Default` the placement
  still says: dialogue is left where the player puts it, a sign is positioned
  over the thing it translates. Without the filter the dub reads out shop
  signs and menus. A cast dub never speaks them. A solo read speaks the ones
  that fall in the clear, because reading out a shop front over an
  establishing shot is part of that register — and never the ones near
  dialogue, because talking across the cast to do it is not.
- **A solo read leans by what the original voice was doing, not by who it
  belongs to.** The pitch of the performance in each line's own span is a
  measurement sitting in the vocals stem, so the actor lifts for a higher
  voice and drops for a lower one with nothing having to identify anybody
  first. Shrunk, snapped and clamped, because a raw per-line measurement
  wanders and the point is a lean rather than an impression.
- **A song is never dubbed.** Karaoke and lyrics styles are dropped outright
  in both modes, which needs naming separately: a lyrics style is neither a
  dialogue style nor a sign, and a solo read that filed it as signage would
  read the opening theme out over itself.
- **Split sentences are rejoined.** A fansubber breaks one spoken line across
  two subtitle events and labels only the first. Those merge into one utterance
  so the voice model gets a whole sentence to find prosody in — including when
  another character speaks between the halves, which is how overlapping
  dialogue gets written down. The rule demands evidence and abstains without
  it, because failing to merge only costs a cold start while merging wrongly
  produces a run-on read in the wrong slot. `dub_script.py --audit` prints the
  rejoins that were inferred from the text and hides the ones the actor field
  stated outright, so the list to review is short by construction.
- **Translator glosses are not spoken.** A fansub writes "That would be the
  daily special (higawari)" so a reader can see the pun the line turns on. Read
  aloud it says a romaji word to an audience that came for English.
- **A unison line is spoken by everyone the label names.** "BEAR/PENGUIN" says
  exactly who speaks, so the line is generated by each of them and laid
  together, staggered slightly and brought down in level — two people never
  start a shared exclamation on the same sample, and stacking identical takes
  exactly reads as one processed voice. Names are matched allowing
  abbreviation, since a label writes "BEAR" for Polar Bear, and anything
  ambiguous resolves to nothing rather than risk miscasting.
- **Crowd lines stay in Japanese.** "EVERYONE" and "BOTH BEARS" name nobody,
  and guessing the cast of a crowd is the kind of confident error the rest of
  this pipeline exists to avoid. So do the lines of any character the voice
  bank had to drop.
- **A line that will not fit is drawn again before it is squeezed.** Most of
  what still overran after the over-long lines were rewritten was not long at
  all: it was short lines the model happened to draw out, four words taking
  three seconds at half its usual rate. That is a bad sample rather than a
  line that cannot fit. Rewriting took episode one from 27 overruns to 13;
  redrawing takes it to 1.
- **The separated bed is used only where a voice is replaced.** Everywhere
  else the original audio plays untouched, crossfading at the edges. This
  keeps separation artefacts out of every stretch that has no dialogue, and it
  is what actually makes an undubbed line keep its original voice: an earlier
  version laid the whole episode over the vocals-removed bed, which silenced
  those lines rather than leaving them alone. The run prints what share of the
  time it took over, and on a talkative episode that is well under half.
- **Each line is matched to the level its character actually spoke at.** The
  vocals stem already says whether a line was muttered or shouted, so the
  generated speech is scaled to that rather than laid down at one level. A dub
  at constant level sits on top of everything and is the giveaway of amateur
  work. Matching is against the scene's own median, not an absolute, and is
  bounded either side: separation is imperfect, and a badly split line reads
  quiet without having been whispered.
- **The original performance stays faintly audible under the dub.** The
  voice-over convention, and it earns its place: a cloned read flattens
  delivery, and hearing the original actor underneath restores the intent even
  when the words are not the ones being followed. The layer is the original
  mix with its centre ducked across the voice band — the trick that predates
  source separation, kept because it is not source separation and so carries
  none of Demucs' smearing. It is added only inside the replaced spans, since
  elsewhere the original is already playing at full level and adding a copy of
  a signal to itself only comb-filters it. `--leak 0` turns it off, `--leak
  0.2` leans into it.
- **The dialogue bus is compressed, gently, on its own.** Matching each line to
  its character fixes the level between lines but not within them, and a
  generated read swings syllable to syllable more than a performance does. It
  runs on the voices alone, before the bed and the leak go in: compressing the
  finished mix would pull the music down every time somebody speaks, which is
  the pumping that gives a bad dub away. The level is restored afterwards so
  the per-line matching is not undone by makeup gain. `--compress 1` disables
  it. On a test bus a 2.5 ratio took the spread from 17.7 dB to 10.8 dB.
- **Timing is fitted after generation, not before.** The released IndexTTS-2
  inference path takes no duration target — it generates whatever length sounds
  natural. So each line is measured, allowed to run into the silence before the
  next speaker, and compressed only when it would collide. Compression stops at
  1.35x; past that a line is left to overlap rather than made to gabble.

## Nudging one character

Characters do not all take the same treatment, and the way to find that out is
to listen. `voices/tuning.json` holds per-character adjustments, applied after
synthesis:

```json
{
 "PANDA": {"air": 0.08, "pitch": -0.5, "gain": 1.1}
}
```

`air` is that character's share of the rebuilt top octave, `pitch` is a nudge
in semitones, `gain` a level trim against the measured match. Try one without
editing the file:

```bash
dub/.venv/bin/python scripts/dub_render.py ... --tune 'PANDA=air:0.08'
```

The first character that needed it was Panda, who came out sounding vocoded.
His voice already carries 11 dB more energy between 5.5 and 11 kHz than Polar
Bear's, so the exciter — which generates the missing octave by squaring that
band — dumped 9 dB more synthetic content on him than on anyone else. The
brightest voice in the cast needs the least air, and gets the most unless told
otherwise.

The air stage is referenced to how bright a character usually is, taken across
all their lines in the render, rather than to the line in hand. Referenced per
line it tracked whatever the model happened to produce, so one character
alternated between robotic and smooth from line to line: measured over a dull,
a normal and a bright rendering of the same voice, the old behaviour varied by
13.3 dB and the character reference holds all three at the same level.

## When the mix sounds wrong but not obviously wrong

```bash
dub/.venv/bin/python scripts/dub_inspect.py dub/preview/cafe_crew.mkv -o inspect.png
```

Both tracks are in the render, and they share a music and effects bed, so
anywhere the dub departs from the original outside the dialogue is the
pipeline doing something it was not asked to. The tool writes original, dubbed
and difference spectrograms, and prints the per-band difference in decibels.

It is a log-mel spectral difference, on purpose. An image-difference metric
such as FLIP models how a person sees a rendered picture; a spectrogram is a
plot rather than something anyone listens to, so a visual metric there scores
the colormap. For a standards-grade perceptual number use ViSQOL or PEAQ. For
finding what broke, a banded difference says where and when, and a single
score never does.

Read the table as a difference rather than as a fault. A dub adds a voice, so
the voice bands read high and are supposed to: on a solo read of Tatami's
opening they come back about 3.7 dB up, which is the reader. The number that
means something is the one in a band no voice is in, and the line below the
table is better still — it compares the bed while a line plays against the bed
between lines. On a cast dub a gap between those two is the score breathing
under the dialogue; on a voice-over it is the duck, and the tool reads the
mode off the timing report rather than calling the design a fault. Pass
`--timing` or neither check runs.

Both spectrograms are referenced to full scale, not each to its own peak.
Referenced to their own, the two are normalised by different amounts and the
difference reports the gap between their peaks: on a render whose tracks peak
3.4 dB apart it called every band 8 to 10 dB out, on a mix whose real
half-second difference ran -2.6 to +5.6 dB around a median of +0.6.

A small constant offset across every band is usually the two tracks' codecs —
the dub is written as AAC beside an AC3 original — rather than anything the
mix did. Ask the waveform if the question is level.

What it found first time out: a cliff at exactly 11 kHz, which is IndexTTS-2's
Nyquist. Below it the dub matched the original to a tenth of a decibel; above
it the dub was 17 dB down, because the model generates at 22.05 kHz and the
original performance carries sibilance well past that. The dialogue read
veiled and every crossfade stepped in brightness. `restore_air` rebuilds that
octave from the band below it, and `--air 0` turns it off.

## Limits worth knowing

Cross-lingual cloning carries timbre and pitch reliably and carries acting
poorly. Expect characters who sound right and read a little flat.

Quality is gated on the subtitle track, not on the model. A sloppy translation
becomes a sloppy dub, spoken confidently. That is truer of a solo read than of
a cast dub, because the whole episode arrives in one voice and there is
nothing else for the ear to attend to.

Songs are not dubbed, and this is enforced rather than left to whoever is
running it: a karaoke or lyrics style is dropped at the parser. Opening,
ending and in-episode singing stay as they are.

## Setup

One-time, into `dub/.venv`:

```bash
uv venv --python 3.11 dub/.venv
uv pip install --python dub/.venv/bin/python torch torchaudio --torch-backend=cu128
uv pip install --python dub/.venv/bin/python numpy demucs soundfile librosa
git clone https://github.com/index-tts/index-tts.git dub/index-tts
uv pip install --python dub/.venv/bin/python -e ./dub/index-tts
dub/.venv/bin/hf download IndexTeam/IndexTTS-2 --local-dir=dub/checkpoints_2
```

`ffmpeg` needs the `rubberband` filter, which is what fits a line to its slot
without the chipmunk artefacts `atempo` leaves on speech.
