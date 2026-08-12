# Dubbing a subbed-only show

Some shows were never dubbed. This pipeline makes an English track for one,
locally, keeping each character's own voice: it clones every speaker from their
original performance and lays the result over the untouched music and effects.

Nothing here touches `library/`. Dubbing works on local copies under `dub/`,
which is gitignored, and writes finished files where you point it.

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
better is worth dubbing. A show where every row says `fallback voice` has no
speaker labels; dubbing it needs speaker diarization, which handles overlapping
anime dialogue badly, and the result is poor. That is the reject signal.

## Running it

Each stage writes files the next stage reads, so a run can be stopped and
resumed. Stages are separate tools because they fail for different reasons and
cost different amounts to repeat.

### 1. Prepare the episodes

Fetches each episode to local disk, splits the audio into a vocals stem and a
music-and-effects bed, and parses the subtitles into utterances.

```bash
python3 scripts/dub_prepare.py "library/tv/Polar Bear Cafe/Season 01" --limit 4
```

Stem splitting runs on the GPU through the `gpu` process queue, so parallel
agent sessions do not oversubscribe the card. Re-running skips finished work.

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
stays stereo; only the voices are mono, and they sit in the centre where screen
dialogue belongs.

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

## What the pipeline decides for you

- **Signage is never spoken.** Fansub styles separate dialogue from typeset
  graphics. Without that filter the dub reads out shop signs and menus.
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
- **Crowd lines stay in Japanese.** No single cloned voice can produce
  "EVERYONE", and there is no clean reference for a crowd by definition. So do
  the lines of any character the voice bank had to drop.
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
becomes a sloppy dub, spoken confidently.

Songs should not be dubbed. Opening, ending and in-episode singing stay as they
are.

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
