#!/usr/bin/env python3
"""Speak every utterance in a cloned voice and mix the dub over the music bed.

The dub replaces only the voices. The music and effects stem from Demucs is
kept as it was, so the score, the ambience and the sound design survive intact
and only the dialogue changes language.

Timing is the hard part. A subtitle window says when text is on screen, not
how long the line takes to say, and the released IndexTTS-2 inference path
offers no target duration: it generates whatever length sounds natural. So
this tool measures each generated line and fits it afterwards. A line is free
to run past its own window into the silence before the next speaker, because
that silence is real room in the scene. Only a line that would collide with
the next speaker is compressed, and only up to the point where compression
stops being audible; past that the line is allowed to overlap rather than be
made to gabble.

Usage:
    python3 scripts/dub_render.py utterances.json voices/ stems/htdemucs/e01.audio \\
        --video source/e01.mkv --from 20:38 --to 21:52 -o preview/cafe.mkv
"""

import argparse
import hashlib
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_align import shift_file  # noqa: E402
from dub_script import PLAIN_ROLES, SOLO_ACTOR, extract_subtitles  # noqa: E402

# Compression beyond this is audible as a gabble, so a line that still does not
# fit is left to overlap the next one instead.
MAX_COMPRESSION = 1.35

# Room left between the end of a dubbed line and the next speaker.
SPEAKER_MARGIN = 0.15

# A global trim on the finished dub, applied after each line has been matched
# to the level the character actually spoke at.
DUB_GAIN, BED_GAIN = 1.0, 0.85

# How far a line may sit from the rest of the scene, as a ratio either side.
# Bounding the gain against fixed limits does not bound the spread, because the
# gain also carries the constant offset between the model's output level and
# the original mix. Judged that way one shouted line came out 18 dB above its
# neighbours; measured against the scene's own median gain it cannot.
MATCH_SPREAD = 2.5

# Separation is imperfect, so a line whose original reads far quieter than the
# rest of the scene is more likely badly split than genuinely whispered. Levels
# are clamped either side of the scene's median before matching.
QUIET_LIMIT, LOUD_LIMIT = 0.4, 2.6

# The separated bed is only used where a voice is actually being replaced.
# Everywhere else the original audio plays untouched, which keeps separation
# artefacts out of the long stretches that have no dialogue, and keeps the
# original voice on any line the dub does not cover.
CROSSFADE = 0.12

# The original voice can start fractionally before its subtitle and ring on
# after it, so the replaced span is widened at both ends to cover it.
REPLACE_PAD = 0.2

# How much of the original mix is left audible under the dub. The voice-over
# convention: the original performance stays faintly present, carrying the
# delivery and the intent that a cloned read flattens, without competing with
# the English for intelligibility.
LEAK_GAIN = 0.12

# Level of the rebuilt top octave, against the band it was generated from.
AIR_AMOUNT = 0.35

# Gentle by intent. Dialogue is always compressed on a real dub stage, but hard
# enough to hear is hard enough to flatten the delivery that was matched
# line by line just above.
COMPRESS_RATIO = 2.5

# How many times a line that cannot fit is generated again before its best
# draw is accepted. Generation is stochastic and short lines occasionally come
# out at half the model's usual rate; a second draw is cheaper and better than
# compressing one of those into a gabble.
RESYNTH_ATTEMPTS = 3

# How far apart two characters start a line they say together. Real unison is
# never sample-aligned, and stacking identical takes exactly reads as one
# processed voice rather than as two people.
UNISON_SPREAD = 0.045

# Every line lands centred unless scripts/dub_overdub.py has a *resolved*
# stereo position for it, so the vast majority of an episode renders exactly
# as it always has.
CENTRE = (1.0, 1.0)

# Screen dialogue is mixed to the centre, so the centre is ducked across the
# band a voice occupies. Outside that band, and anywhere off centre, the score
# and the effects are left alone.
LEAK_BAND = (300.0, 3400.0)

# What survives of the centre inside that band. Not zero: cancelling the centre
# outright also takes the sound design sitting behind the actor, and the point
# is to hear the performance quietly, not to remove it twice.
LEAK_CENTRE = 0.3

# How far a solo actor moves to suggest a different character: a semitone or
# two, a few percent of pace, a little level. Small on purpose. The conceit is
# one person doing all the voices, and a wide swing stops reading as that
# person acting and starts reading as a second actor badly spliced in.
SHADE_PITCH, SHADE_PACE, SHADE_GAIN = 1.6, 0.06, 0.10

# Pitch is tracked once across the whole episode, at this rate and hop, and
# every line then reads its own span out of the result. Downsampled because
# nothing above 500 Hz is being looked for and the tracker costs what it is
# given.
PITCH_RATE, PITCH_FRAME, PITCH_HOP = 16000, 1024, 256

# A line with fewer voiced frames than this has not been measured, it has been
# guessed at. Roughly a third of a second of actual voice.
MIN_VOICED = 20

# How many semitones of real difference it takes to reach most of the lean,
# through a curve rather than a straight line. A man and a woman are fifteen
# semitones apart, so anything proportional puts every line of both at the
# clamp and the shade becomes a two-position switch — measured that way, 248
# of 329 lines sat on a rail. A curve keeps small differences small and lets
# large ones approach the limit without every one of them arriving there.
SHADE_SCALE = 8.0

# Shades snap to this many semitones. Two lines of one character measured a
# hair apart should land on the same shade, or the read wanders inside a scene
# in a way no person does.
SHADE_STEP = 0.25

# How wide a band of pitch counts as one voice when looking for the register
# the episode mostly sits in, in semitones either side.
NEUTRAL_BAND = 2.0

# A sign is read rather than performed: the actor is telling you what it says,
# a touch flatter, a touch quicker and out of the way.
SIGN_SHADE = {"pitch": -0.3, "pace": 1.05, "gain": 0.9}

# How far the original mix comes down while the voice-over is speaking. The
# broadcast figure, and it is a duck rather than a replacement: the original
# performance stays there through the whole line, which is the whole texture
# of a one-voice dub.
DUCK_DB = 11.0

# A voice-over comes in behind the line it is translating, never on top of its
# first syllable. Small enough to read as the same beat, large enough that both
# onsets are audible.
VOICEOVER_LAG = 0.2

# Breath between two lines one person reads back to back.
SOLO_GAP = 0.12

# How late a queued line may start before being allowed to overlap instead.
# Past this it is answering a shot that has already gone, and a moment of two
# voices is the smaller error.
MAX_LAG = 2.5

# Exit code for a render that stopped because it was asked to. Distinct from a
# failure, because everything it had drawn is on disk and the next run carries
# straight on from there.
EXIT_PAUSED = 75


class Paused(Exception):
    """A stop asked for between lines. What was drawn already is on disk."""


_stop_asked = False


def watch_for_stop(pause_file=None):
    """Let a signal, or a file, ask this render to stop between lines.

    Both, because they answer different situations. A signal is what the
    session that started the render has to hand. The file is what a session
    which does not own the process can reach, and that is the one that matters
    when the stop is asked for days later from somewhere else.
    """
    def stop(number, frame):
        global _stop_asked
        if _stop_asked:
            raise KeyboardInterrupt      # asked twice means now, cache be damned
        _stop_asked = True
        print("\nstopping after the line being drawn", flush=True)

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    return Path(pause_file) if pause_file else None


def stop_asked(pause_file):
    return _stop_asked or (pause_file is not None and pause_file.exists())


# Punctuation the tokenizer has no token for. The horizontal bar and the minus
# sign are the two that need naming: NFKC leaves both exactly as they are,
# having nothing to decompose them to. The ellipsis and the no-break space NFKC
# would fold on its own, and stay here for whoever reads the map. The em dash is
# absent deliberately: the tokenizer already takes it as a hyphen.
UNSPEAKABLE = {"―": "-", "−": "-", "…": "...", " ": " "}


def speakable(text, say_as):
    """The line as the model should receive it, which is not what a reader gets.

    This used to fold every accent away before synthesis. On IndexTTS-2 that was
    right: its tokenizer had seen English and nothing else, an accented letter
    was in no pair it knew, so `café` arrived as CA, F, É and the model — handed
    a character with no English sound — guessed, which is where "caf" and "cafu"
    came from.

    2.5's tokenizer is multilingual and byte-level. It holds ` café` whole, as
    one piece it has heard, and folding that to ` cafe` hands it a *different*
    token to guess at. Measured over this show, folding wrecked far more than it
    rescued — `caffè` became "calf latte", `à la mode` became "Allah mode" — so
    the line now goes to the model as written, and the exceptions are named
    rather than assumed.

    An exception is a word the model reads wrong as written, which is a fact
    about that word and only findable by listening: `dub_saytest.py` speaks the
    candidates and `dub_sayhear.py` transcribes them. The answer goes in the
    lexicon, either as a plain respelling or as an ARPABET annotation 2.5 is
    trained to obey — `<göreme|G ER1 EH0 M EH0>`.

    Compatibility forms are still normalised, which is a different thing from
    folding accents and was worth keeping when the fold went: a fansub that
    types a fullwidth hyphen or a halfwidth katakana means the ordinary
    character, and NFKC says so. It leaves `é` as `é` — stripping that needs
    NFKD and a pass over the combining marks, which is exactly what no longer
    happens here.

    Only synthesis sees any of this. The subtitle track keeps the real word.
    """
    text = unicodedata.normalize("NFKC",
                                 "".join(UNSPEAKABLE.get(c, c) for c in text))
    if not say_as:
        return text

    def swap(match):
        word = match.group(0)
        spoken = say_as[word.lower()]
        # A respelling is lower case in the lexicon; a word that was capitalised
        # in the line stays capitalised, so sentence case survives. An ARPABET
        # annotation is unharmed by this — the model upper-cases the phones
        # itself, and discards the word left of the bar entirely.
        return spoken.capitalize() if word[:1].isupper() else spoken

    return re.sub(r"\b(?:%s)\b" % "|".join(map(re.escape, say_as)),
                  swap, text, flags=re.IGNORECASE)


def clip_stamp(text, references, fits, emo):
    """What a cached clip was drawn from, so a later run knows to trust it.

    A clip is only worth keeping if the next run would ask for the same thing.
    The reference is stamped by its size as well as its name, so a rebuilt
    voice bank redraws the character rather than quietly reusing the old one,
    while a bank merely re-saved unchanged does not cost an episode of GPU.

    Lists rather than tuples throughout, because the stamp is compared against
    its own JSON: a tuple written out comes back a list and never matches what
    wrote it, which silently costs the whole cache.
    """
    return {"text": text, "emo": bool(emo), "fits": round(fits, 2),
            "voices": sorted([str(path), Path(path).stat().st_size]
                             for path in references)}


def parse_timecode(value):
    if value is None:
        return None
    parts = str(value).split(":")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def fit_to_slot(clip_path, generated, available, rate, semitones=0.0, pace=1.0):
    """Compress a generated line only as far as it stays natural.

    The returned audio is always at `rate`, whether or not it was compressed.
    Resampling has to happen in exactly one place: when the caller resampled a
    second time on the compression path, every compressed line played an octave
    low and at twice the length, running over the next speaker.

    `pace` is a deliberate change of speed on top of the fitting, which is how
    a solo actor colours one character against another. It rides on the same
    tempo control rather than a second pass, because two rubberband passes over
    one line smear it twice.
    """
    factor = 1.0
    if available > 0.2 and generated > available:
        factor = min(generated / available, MAX_COMPRESSION)

    settings = []
    if abs(factor * pace - 1.0) > 0.001:
        settings.append(f"tempo={factor * pace:.4f}")
    if semitones:
        settings.append(f"pitch={2 ** (semitones / 12.0):.5f}")

    fitted = Path(tempfile.mkdtemp()) / "fit.wav"
    shaped = ["-af", "rubberband=" + ":".join([*settings, "pitchq=quality"])] if settings else []
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip_path), *shaped,
                    "-ar", str(rate), "-ac", "1", str(fitted), "-y"], check=True)

    audio, produced = sf.read(fitted, dtype="float32", always_2d=True)
    if produced != rate:
        raise SystemExit(f"fit produced {produced} Hz, expected {rate}")
    return audio.mean(axis=1), factor


def resolve_members(members, bank):
    """Match the names in a group label to banked voices.

    A label abbreviates: "BEAR" and "P.BEAR" both mean Polar Bear. A name
    matches when each of its words begins a word of the banked name, which
    accepts those and rejects a name that merely shares a letter. Anything
    ambiguous resolves to nothing, because casting a unison line to the wrong
    character is worse than leaving it in the original.
    """
    import re

    resolved = []
    for member in members:
        wanted = [word for word in re.split(r"[^A-Za-z0-9]+", member.upper()) if word]
        if not wanted:
            return []

        candidates = []
        for name in bank:
            available = [word for word in re.split(r"[^A-Za-z0-9]+", name.upper()) if word]
            taken, ok = set(), True
            for word in wanted:
                hit = next((other for other in available
                            if other not in taken and other.startswith(word)), None)
                if hit is None:
                    ok = False
                    break
                taken.add(hit)
            if ok:
                candidates.append(name)

        if len(candidates) != 1:
            return []
        resolved.append(candidates[0])

    return resolved


def shade_for(role):
    """A small, repeatable colour for one role, taken from its name.

    Nothing here knows anything about the character, so this only has two
    jobs: keep two roles from coming out identical, and keep one role sounding
    the same in episode nine as in episode one. A hash of the name does both
    for free.

    It is the floor rather than the answer. Pitch is measured off the original
    performance wherever that is possible — see `measured_shades` — because
    the one thing a hash cannot get right is which way to move.
    """
    digest = hashlib.sha1(role.encode("utf-8")).digest()
    spread = [(byte / 127.5) - 1.0 for byte in digest[:3]]
    return {"pitch": round(spread[0] * SHADE_PITCH, 2),
            "pace": round(1.0 + spread[1] * SHADE_PACE, 3),
            "gain": round(1.0 + spread[2] * SHADE_GAIN, 3)}


def track_pitch(voices, rate):
    """Voiced pitch across the whole episode, measured once.

    Once rather than per line, because the tracker's cost is in the audio it
    is handed and the lines are most of the episode anyway. Every line then
    reads its own span out of the result.
    """
    import librosa

    mono = voices.mean(axis=1)
    if rate != PITCH_RATE:
        mono = librosa.resample(mono, orig_sr=rate, target_sr=PITCH_RATE)

    # pyin rather than yin, for the voicing decision rather than the pitch.
    # yin answers for every frame including the ones holding no voice at all,
    # and a separated stem is full of those: gated on energy alone it called
    # 66% of the episode voiced and put the neutral 28 Hz above where the cast
    # actually speak, because it was averaging in the pitch of leaked music.
    f0, voiced, _ = librosa.pyin(mono, fmin=60, fmax=500, sr=PITCH_RATE,
                                 frame_length=PITCH_FRAME, hop_length=PITCH_HOP)
    return f0, voiced & np.isfinite(f0), PITCH_HOP / PITCH_RATE


def dominant_pitch(spoken):
    """The register the episode mostly sits in, from its measured lines.

    The biggest cluster, not the middle of the spread. A show puts many short
    lines from many mouths around one voice that holds the floor, and the
    middle of that spread is a pitch nobody actually speaks at: measured that
    way, a narrated show came out with its narrator 32 Hz *below* neutral, so
    every line of his read pitched down. The densest band, weighted by how
    long each line is, lands on the voice the show sounds like — which is also
    the voice the reference was cut from, so a solo read's own lines come out
    unshaded, which is the point of a neutral.
    """
    keys = np.log2(np.asarray([pitch for pitch, _ in spoken])) * 12.0
    weight = np.asarray([window for _, window in spoken])
    held = [float(weight[np.abs(keys - key) <= NEUTRAL_BAND].sum()) for key in keys]
    return float(2 ** (keys[int(np.argmax(held))] / 12.0))


def line_shades(utterances, f0, voiced, hop):
    """How far each line's own original performance sits from the show's own
    neutral, in semitones the actor should lean by.

    This is the whole of the shading, and it needs no idea who is speaking.
    The original track already says what pitch the character is at in this
    exact moment, so the read leans that way — up for the girl, down for the
    big man — without anything having to name them first. Where a labelling
    does exist it only steadies the result: a line too short or too buried to
    measure borrows its role's median rather than falling flat.

    Measured against the episode's own median voiced pitch rather than
    against the actor, so a shade is a property of the show and travels
    between actors. Against the actor it would be systematically wrong the
    moment they were cast from somewhere else: a woman reading a show narrated
    by a man would find every ordinary line pulling her down toward him.

    Stabilised three ways, because a raw per-line measurement wanders. It is
    shrunk, so the actor suggests the difference instead of chasing it; it is
    snapped to a step, so two lines of one character measured a hair apart
    land on the same shade; and it is clamped, so nothing becomes an
    impression however far apart the two voices really are.
    """
    heard, measured, spoken = {}, {}, []
    for utterance in utterances:
        # A sign is words on the picture. Whatever is voiced under it is not
        # saying them, so its span measures somebody else entirely.
        if utterance.get("kind") == "sign":
            continue
        head, tail = int(utterance["start"] / hop), int(utterance["end"] / hop)
        window = f0[head:tail][voiced[head:tail]]
        window = window[np.isfinite(window) & (window > 0)]
        if window.size >= MIN_VOICED:
            measured[utterance["id"]] = float(np.median(window))
            spoken.append((measured[utterance["id"]], utterance["window"]))
            if utterance.get("role"):
                heard.setdefault(utterance["role"], []).append(measured[utterance["id"]])

    if not measured:
        return {}, {}, 0.0

    neutral = dominant_pitch(spoken)
    roles = {role: float(np.median(pitches)) for role, pitches in heard.items()
             if len(pitches) >= 2}

    def shade(pitch):
        apart = 12.0 * np.log2(pitch / neutral)
        leaned = SHADE_PITCH * np.tanh(apart / SHADE_SCALE)
        stepped = round(leaned / SHADE_STEP) * SHADE_STEP
        return round(float(np.clip(stepped, -SHADE_PITCH, SHADE_PITCH)), 2)

    lifts = {}
    for utterance in utterances:
        pitch = measured.get(utterance["id"]) or roles.get(utterance.get("role"))
        if pitch:
            lifts[utterance["id"]] = shade(pitch)

    return lifts, {role: (shade(pitch), round(pitch)) for role, pitch in roles.items()}, neutral


def nudges_for(utterance, tuning, shades, lifts=None):
    """Compose what the actor asks for with what this line asks for.

    The tuning entry is the actor's own setting and applies to everything they
    say. The shade goes on top: pitch adds, pace and gain multiply, so a shade
    moves the actor from wherever they were rather than overwriting a tuning
    arrived at by ear.

    Pitch comes from `lifts` — this line's own measured distance from the
    actor's register — whenever the line could be measured. A role's hashed
    shade supplies pace and gain, and supplies pitch only where nothing was
    measurable, which is the case a name is the last thing left to go on.
    """
    base = dict(tuning.get(utterance["speaker"], {}))
    role, lift = utterance.get("role"), (lifts or {}).get(utterance.get("id"))

    if utterance.get("kind") == "sign":
        shade = SIGN_SHADE
    elif role and role not in PLAIN_ROLES:
        shade = shades.get(role, shade_for(role))
    elif lift is not None:
        # No label, and none needed: the original performance in this span is
        # the only thing the shade was ever really about.
        shade = {}
    else:
        # Narration is not a character the actor is doing, it is the actor.
        # Colouring it would leave the episode with no neutral to hear the
        # coloured lines against, which is what makes a shade read as a shade.
        return base

    pitch = lift if lift is not None else shade.get("pitch", 0.0)
    return {**base,
            "pitch": base.get("pitch", 0.0) + pitch,
            "pace": base.get("pace", 1.0) * shade.get("pace", 1.0),
            "gain": base.get("gain", 1.0) * shade.get("gain", 1.0)}


def pan_gains(pan):
    """Equal-power left/right gain for a resolved overdub position.

    -1 is hard left, +1 hard right, 0 dead centre. This is the proper mixing
    law for an explicit pan choice, and it is deliberately not what a line
    gets by default: `CENTRE` puts the same sample in both channels at full
    gain, which is what every line in this pipeline has always done, and nothing
    changes that unless a case has been resolved for it.
    """
    pan = min(1.0, max(-1.0, pan))
    angle = (pan + 1.0) * (np.pi / 4.0)
    return float(np.cos(angle)), float(np.sin(angle))


def room_for(utterance):
    """Seconds a line may occupy before it collides with the next speaker."""
    return utterance["window"] + max(0.0, utterance["slack"] - SPEAKER_MARGIN)


def synthesize(speak, utterances, bank, workdir, emo_from_text, attempts=RESYNTH_ATTEMPTS,
               clips=None, pause_file=None, say_as=None):
    """Generate one wav per utterance, in that character's cloned voice.

    A line that will not fit even at the compression ceiling is generated
    again rather than squeezed. The overruns left after rewriting the
    over-long lines were mostly short ones the model happened to draw out —
    four words taking three seconds, at half the rate it usually speaks. That
    is a bad sample from a stochastic model, not a line that is too long, and
    the answer to a bad sample is another sample.

    This is the whole GPU cost of an episode and the only slow part of it, so
    it is also the only part worth being able to stop inside. With a clip
    directory the lines already drawn stay drawn, and a stop costs the one line
    in the model's hands rather than the quarter hour behind it.
    """
    rendered, reused, respelled = [], 0, 0
    for utterance in utterances:
        if stop_asked(pause_file):
            raise Paused()

        # A line somebody has marked as not to be spoken. The parser cannot
        # tell a translator's note from an aside, so this is where a reading
        # of the episode gets to say so.
        if utterance.get("kind") == "skip":
            continue
        speaking = ([utterance["speaker"]] if not utterance["group"]
                    else resolve_members(utterance.get("members", []), bank))
        if not speaking or any(name not in bank for name in speaking):
            if utterance["group"]:
                continue                   # a crowd: left in the original audio
            print(f"  no voice for {utterance['speaker']}, left in the original language")
            continue

        kwargs = {"use_emo_text": True, "emo_alpha": 0.8} if emo_from_text else {}
        fits = room_for(utterance) * MAX_COMPRESSION

        # What the model is given, which is not always what the subtitle says.
        # Stamped on the clip, so changing how a word is spoken redraws the
        # lines carrying it and leaves the rest of the episode alone.
        spoken = speakable(utterance["text"], say_as)
        stamp = clip_stamp(spoken, [bank[name]["path"] for name in speaking],
                           fits, kwargs)
        drawn = (clips or workdir) / f"{utterance['id']:04d}.wav"
        beside = drawn.with_suffix(".json")
        if clips and drawn.exists() and beside.exists():
            if json.loads(beside.read_text()) == stamp:
                rendered.append((utterance, drawn))
                reused += 1
                continue

        takes, retries = [], 0
        for name in speaking:
            take = draw_line(speak, bank[name]["path"], spoken, fits,
                             kwargs, attempts)
            if take is None:
                continue
            takes.append(take[:2])
            retries = max(retries, take[2])

        if not takes:
            print(f"  synthesis produced nothing for line {utterance['id']}")
            continue

        audio, sample_rate = (takes[0] if len(takes) == 1 else lay_together(takes))

        # The wav appears whole or not at all, and its stamp is written only
        # once it has. A clip caught half-written by a kill has nothing beside
        # it claiming it is finished, so the next run draws it again instead of
        # mixing in a truncated line.
        staged = drawn.with_suffix(".part")
        sf.write(staged, audio, sample_rate, format="WAV")
        staged.replace(drawn)
        if clips:
            beside.write_text(json.dumps(stamp))
        rendered.append((utterance, drawn))

        note = f"  (redrawn {retries}x)" if retries else ""
        if len(takes) > 1:
            note = f"  (in unison: {', '.join(speaking)}){note}"
        if spoken != utterance["text"]:
            respelled += 1
        print(f"  [{utterance['id']:>3}] {utterance['speaker']:<12} "
              f"{utterance['text'][:52]}{note}")

    if reused:
        print(f"  kept {reused} lines drawn before the last stop")
    if respelled:
        print(f"  respelled {respelled} lines for the tokenizer; the subtitle keeps the real words")
    return rendered


def draw_line(speak, voice_path, text, fits, kwargs, attempts):
    """Generate one line, redrawing while it will not fit."""
    best, retries = None, 0
    for attempt in range(attempts):
        # Asking for no output path returns the audio instead of writing it.
        # Writing it here keeps the pipeline off torchaudio.save, which needs
        # TorchCodec on current torchaudio and fails without it.
        result = speak(spk_audio_prompt=voice_path, text=text,
                       output_path=None, verbose=False, **kwargs)
        if result is None:
            continue
        sample_rate, samples = result
        # The model hands back a column, one sample per row. soundfile writes
        # that as mono without complaint, so the shape went unnoticed until two
        # takes had to be summed and a column broadcast against a row.
        audio = np.asarray(samples).astype("float32").reshape(-1) / 32768.0
        length = audio.size / sample_rate
        if best is None or length < best[0]:
            best = (length, audio, sample_rate)
        if length <= fits or fits <= 0.2:
            break
        retries = attempt + 1

    return (best[1], best[2], retries) if best else None


def lay_together(takes):
    """Stack several characters saying the same line at once.

    Two people never start a shared exclamation on the same sample, and laying
    identical-length takes exactly on top of each other reads as one processed
    voice rather than two. A short stagger is what makes it a pair. Levels come
    down together because uncorrelated voices sum louder than one.
    """
    rate = takes[0][1]
    stagger = int(UNISON_SPREAD * rate)
    length = max(audio.size for audio, _ in takes) + stagger * len(takes)

    stacked = np.zeros(length, dtype="float32")
    for index, (audio, _) in enumerate(takes):
        head = index * stagger
        stacked[head:head + audio.size] += audio
    return stacked / np.sqrt(len(takes)), rate


def speech_level(samples, rate, floor=0.15):
    """Loudness of the speech in a clip, ignoring the silence around it.

    Plain RMS over a whole line rates a short word in a long subtitle window
    as quiet, because it averages in the pauses. Only the frames carrying
    speech are counted, so the number describes the delivery rather than the
    timing.
    """
    if samples.ndim > 1:
        samples = samples.mean(axis=1)     # a stereo dub bus, judged as heard
    width = max(1, int(0.02 * rate))
    usable = (samples.size // width) * width
    if usable == 0:
        return 0.0
    energy = np.sqrt(np.mean(np.square(samples[:usable].reshape(-1, width)), axis=1))
    if not energy.size or energy.max() <= 0:
        return 0.0
    loud = energy[energy > floor * energy.max()]
    return float(loud.mean()) if loud.size else float(energy.mean())


def air_band(audio, rate):
    """The band the rebuilt octave is generated from, and its loudness."""
    from scipy import signal

    nyquist = rate / 2.0
    if nyquist <= 11500:
        return None, 0.0
    edges = [5500.0 / nyquist, min(10800.0 / nyquist, 0.99)]
    numerator, denominator = signal.butter(4, edges, btype="bandpass")
    band = signal.filtfilt(numerator, denominator, audio)
    return band, float(np.sqrt(np.mean(np.square(band))))


def restore_air(audio, rate, amount, reference=None):
    """Rebuild the top octave the voice model cannot produce.

    IndexTTS-2 generates at 22.05 kHz, so its output holds nothing above
    11 kHz, while the original performance carries sibilance and breath well
    past it. Laid into a 44.1 kHz mix the dialogue reads veiled, and every
    crossfade between untouched original and replaced span steps in brightness.

    The missing octave is synthesised from the band below it: a non-linearity
    generates harmonics of 5.5-11 kHz, which land in 11-22 kHz and follow the
    speech that produced them. It is not the original actor's sibilance, but it
    is in the right place at the right time, which is what the ear is listening
    for up there.
    """
    from scipy import signal

    if amount <= 0:
        return audio

    nyquist = rate / 2.0
    band, level = air_band(audio, rate)
    if band is None:
        return audio                    # nothing above the model's own ceiling

    harmonics = band * np.abs(band)     # squared, keeping the sign
    numerator, denominator = signal.butter(4, 11000.0 / nyquist, btype="highpass")
    harmonics = signal.filtfilt(numerator, denominator, harmonics)

    # Squaring piles its output around twice the source band, so the raw result
    # is brightest at the very top where speech is faintest. Left that way it
    # trades a dull dub for a fizzy one. A gentle roll-off restores the downward
    # slope real sibilance has.
    numerator, denominator = signal.butter(1, min(12500.0 / nyquist, 0.99),
                                           btype="lowpass")
    harmonics = signal.filtfilt(numerator, denominator, harmonics)

    loudness = np.sqrt(np.mean(np.square(harmonics)))
    if loudness <= 0:
        return audio

    # Referenced to what this character usually sounds like, not to what this
    # line happened to come out as. Scaling to each line's own band makes the
    # rebuilt octave loud on a line the model rendered bright and quiet on one
    # it rendered dull, so a character alternates between robotic and smooth
    # across a scene. `reference` carries the character's habitual level.
    target = (level if reference is None else reference) * amount
    return (audio + harmonics * (target / loudness)).astype("float32")


def compress_dialogue(dub, rate, ratio):
    """Even the dialogue out, the way a dub stage would.

    Matching each line to what the character spoke at fixes the level between
    lines but not within them, and a generated read still swings syllable to
    syllable more than a performance does. A gentle ratio closes that up.

    It runs on the voice bus alone, before the bed and the leak are added.
    Compressing the finished mix instead would pull the music down every time
    somebody speaks, which is the pumping that gives a bad dub away.

    The level is put back afterwards: compression lowers what it touches, and
    the per-line matching that ran earlier is worth more than the makeup gain
    guess a compressor would apply.
    """
    if ratio <= 1.0:
        return dub

    before = speech_level(dub, rate)
    workdir = Path(tempfile.mkdtemp())
    sf.write(workdir / "pre.wav", dub, rate)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(workdir / "pre.wav"),
                    "-af", f"acompressor=threshold=0.1:ratio={ratio:.2f}"
                           ":attack=8:release=180:knee=6:makeup=1",
                    str(workdir / "post.wav"), "-y"], check=True)

    evened, produced = sf.read(workdir / "post.wav", dtype="float32", always_2d=True)
    if produced != rate:
        raise SystemExit(f"compressor produced {produced} Hz, expected {rate}")

    after = speech_level(evened, rate)
    if after > 0:
        evened = evened * (before / after)
    return evened[:dub.shape[0]].astype("float32")


def duck_centre(original, rate):
    """The original mix with its dialogue pushed back, everything else intact.

    The trick that predates source separation, and it earns its place here
    precisely because it is not source separation: it takes the centre down
    across the voice band and leaves the waveform otherwise untouched, so the
    result carries none of the smearing that Demucs leaves behind. That matters
    for a layer meant to sit under the dub, where an artefact is more audible
    than the voice it came from.
    """
    from scipy import signal

    if original.shape[1] < 2:
        return original * LEAK_CENTRE

    mid = original.mean(axis=1)
    side = (original[:, 0] - original[:, 1]) / 2.0

    nyquist = rate / 2.0
    band = [LEAK_BAND[0] / nyquist, min(LEAK_BAND[1] / nyquist, 0.99)]
    numerator, denominator = signal.butter(2, band, btype="bandpass")
    voice_band = signal.filtfilt(numerator, denominator, mid)

    ducked = mid - (1.0 - LEAK_CENTRE) * voice_band
    return np.stack([ducked + side, ducked - side], axis=1).astype("float32")


def build_track(rendered, bed, voices, rate, air, tuning, overdubs=None,
                shades=None, sequential=False, lag=0.0, lifts=None):
    """Lay each spoken line onto a silent track at its scene position.

    The bed stays stereo because it carries the score and the sound design,
    and folding it to mono to match the voices would throw away the stereo
    image of the original mix. The generated speech is mono and almost always
    goes to the centre, which is where screen dialogue belongs — except for a
    line inside a resolved overdub case, which `overdubs` places instead.

    `sequential` queues the lines instead of placing each at its own start.
    One actor cannot talk over themselves: two of their lines summed on top of
    each other are not two voices, they are one voice made unintelligible. So
    a solo read waits its turn, and catches up by speaking the queued line
    into whatever room is left rather than by staying behind.
    """
    dub = np.zeros((bed.shape[0], 2), dtype="float32")
    report, geometry, lines = [], [], []
    overdubs, shades = overdubs or {}, shades or {}
    cursor = 0

    for utterance, clip_path in rendered:
        probe = sf.info(str(clip_path))
        generated = probe.frames / probe.samplerate

        head = int((utterance["start"] + lag) * rate)
        if sequential and head < cursor and (cursor - head) / rate <= MAX_LAG:
            head = cursor
        late = head / rate - utterance["start"]

        # Whatever the queue ate comes off the room this line has, so the read
        # speeds up to make it back rather than pushing the lateness into the
        # next line and the one after that.
        available = max(0.0, utterance["window"] - late
                        + max(0.0, utterance["slack"] - SPEAKER_MARGIN))

        # Per-character nudges. Characters do not all take the same treatment:
        # the brighter a voice, the more the air stage colours it, and a high
        # voice given the default reads as though it went through a vocoder.
        # On a solo read the line's role rides on top of them.
        nudge = nudges_for(utterance, tuning, shades, lifts)
        audio, factor = fit_to_slot(clip_path, generated, available, rate,
                                    nudge.get("pitch", 0.0), nudge.get("pace", 1.0))
        cursor = head + audio.size + int(SOLO_GAP * rate)

        spoken = voices[int(utterance["start"] * rate):int(utterance["end"] * rate)]
        produced = speech_level(audio, rate)
        lines.append({"utterance": utterance, "audio": audio, "factor": factor,
                      "generated": generated, "available": available,
                      "head": head, "late": late, "nudge": nudge,
                      "target": speech_level(spoken.mean(axis=1), rate),
                      "produced": produced,
                      "brightness": (air_band(audio, rate)[1] / produced) if produced else 0.0})

    # How bright each character usually is, so the rebuilt octave is referenced
    # to the character rather than to one line's accident of rendering.
    habit = {}
    for line in lines:
        habit.setdefault(line["utterance"]["speaker"], []).append(line["brightness"])
    habit = {speaker: float(np.median(values)) for speaker, values in habit.items()}

    for line in lines:
        speaker = line["utterance"]["speaker"]
        amount = tuning.get(speaker, {}).get("air", air)
        line["audio"] = restore_air(line["audio"], rate, amount,
                                    reference=line["produced"] * habit.get(speaker, 0.0))

    # The scene's own median is the reference point, so a line is matched
    # against how loudly this cast is speaking here rather than an absolute.
    levels = [line["target"] for line in lines if line["target"] > 0]
    middle = float(np.median(levels)) if levels else 0.0

    for line in lines:
        raw = 1.0
        if middle > 0 and line["target"] > 0 and line["produced"] > 0:
            wanted = min(max(line["target"], QUIET_LIMIT * middle), LOUD_LIMIT * middle)
            raw = wanted / line["produced"]
        line["raw_gain"] = raw

    # Centred on what this scene needs overall, so the bound limits how far
    # lines sit apart rather than where they all sit.
    centre = float(np.median([line["raw_gain"] for line in lines])) if lines else 1.0

    for line in lines:
        utterance, audio = line["utterance"], line["audio"]
        gain = min(max(line["raw_gain"], centre / MATCH_SPREAD), centre * MATCH_SPREAD)
        gain *= line["nudge"].get("gain", 1.0)

        placement = overdubs.get(utterance["id"])
        if placement is not None:
            left, right = pan_gains(placement["pan"])
            gain *= 10 ** (placement.get("gain_db", 0.0) / 20.0)
        else:
            left, right = CENTRE

        head = min(line["head"], dub.shape[0])
        tail = min(head + audio.size, dub.shape[0])
        clip = audio[:tail - head] * gain
        dub[head:tail, 0] += clip * left
        dub[head:tail, 1] += clip * right

        # The span to take over from the original covers both the line as
        # spoken in Japanese and the dub that replaces it, whichever runs
        # longer, so no part of the original voice is left ringing underneath.
        # A queued line starts after its own subtitle, so the span reaches back
        # to the subtitle: otherwise the original's first words play in the
        # clear before the dub arrives to cover them.
        pad = int(REPLACE_PAD * rate)
        geometry.append({
            "speaker": utterance["speaker"], "head": head, "tail": tail,
            "replace_head": max(0, min(head, int(utterance["start"] * rate)) - pad),
            "replace_tail": min(dub.shape[0], max(tail, int(utterance["end"] * rate)) + pad)})

        report.append({"id": utterance["id"], "speaker": utterance["speaker"],
                       "role": utterance.get("role"),
                       # Where the line ended up and how long it holds, which
                       # is what dub_inspect.py needs to tell the bed moving
                       # from speech being added. It read neither before, so
                       # its bed check silently never ran.
                       "start_seconds": round(head / rate, 3),
                       "held": round((tail - head) / rate, 3),
                       "generated": round(line["generated"], 2),
                       "available": round(line["available"], 2),
                       "compression": round(line["factor"], 3),
                       "gain": round(float(gain), 3),
                       "late": round(line["late"], 3),
                       "pan": round(placement["pan"], 3) if placement else 0.0,
                       "overflow": round(max(0.0, line["generated"] / line["factor"]
                                             - line["available"]), 2)})

    return dub, report, geometry


def subtitle_source(args, workdir):
    """The subtitle track the dub was built from, to travel with it.

    Taken from the video by default, which is right whenever the dub was
    parsed from the release's own track. Where it was parsed from a subtitle
    file cut for another release, that file is the one that belongs in the
    output and `--subtitles` names it — the video's own track would be a
    different translation at a different offset.
    """
    if args.subtitles:
        return Path(args.subtitles)
    if not args.video:
        return None
    try:
        return extract_subtitles(args.video)
    except SystemExit:
        return None                 # a release with no text subtitles at all


def write_spoken_subtitles(report, utterances, path, offset=0.0):
    """A subtitle track of what the dub actually said, where it said it.

    Not the same thing as the track the dub was built from, and the difference
    is the point. Lines get rewritten to fit the time available, and a queued
    line is spoken after its own subtitle, so the source track answers "what
    does this scene mean" while this one answers "what did it just say" — the
    question you have when a generated line comes out wrong.
    """
    by_id = {utterance["id"]: utterance for utterance in utterances}

    def stamp(seconds):
        hours, rest = divmod(max(0.0, seconds), 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}".replace(".", ",")

    blocks = []
    for index, row in enumerate(sorted(report, key=lambda r: r["start_seconds"]), start=1):
        utterance = by_id.get(row["id"])
        if utterance is None:
            continue
        head = row["start_seconds"] - offset
        blocks.append(f"{index}\n{stamp(head)} --> {stamp(head + row['held'])}\n"
                      f"{utterance['text']}\n")

    Path(path).write_text("\n".join(blocks), encoding="utf-8")
    return len(blocks)


def duck_original(original, dub, rate, depth_db, span=None):
    """Pull the original mix down under the voice-over, and let it back up.

    This is the other way to build a dub, and the one a single actor has
    always been laid over: nothing is replaced. The original plays the whole
    way through — its performances, its score, its effects — and simply steps
    back while the voice-over talks.

    It is the two stems summed rather than either one used alone, which is the
    point: summing undoes the split, so whatever Demucs smeared going one way
    is cancelled by the complementary smear going the other. A cast dub has to
    keep the bed on its own and lives with that; here it never arises.

    A duck has to be quick down and slow up. Quick, or the first syllable of
    the translation lands under the original at full level; slow, or the score
    lifts back into every gap between two lines and flutters.
    """
    frame = max(1, int(0.01 * rate))
    mono = np.abs(dub).mean(axis=1) if dub.ndim > 1 else np.abs(dub)
    usable = (mono.size // frame) * frame
    envelope = mono[:usable].reshape(-1, frame).max(axis=1)

    floor = 10 ** (-abs(depth_db) / 20.0)
    speaking = envelope > 0.08 * speech_level(dub, rate)
    target = np.where(speaking, floor, 1.0)

    falling = np.exp(-1.0 / max(1.0, 0.03 * rate / frame))
    rising = np.exp(-1.0 / max(1.0, 0.45 * rate / frame))
    gain, current = np.empty_like(target), 1.0
    for index, wanted in enumerate(target):
        current = wanted + (current - wanted) * (falling if wanted < current else rising)
        gain[index] = current

    curve = np.interp(np.arange(original.shape[0]),
                      np.arange(gain.size) * frame + frame / 2.0, gain,
                      left=1.0, right=1.0).astype("float32")

    # Reported over the span actually exported. Against the whole episode a
    # preview of one scene reports how little of the episode that scene is,
    # which reads as a broken duck rather than as a short preview.
    counted = speaking[span[0] // frame:span[1] // frame] if span else speaking
    return original * curve[:, None], float(counted.mean()) if counted.size else 0.0


def replacement_mask(geometry, length, rate):
    """Weight of the dubbed mix against the original, sample by sample.

    One where a voice is being replaced, zero where the original should play,
    with a short ramp between. The bed is a component of the original rather
    than something independent, so the two sides stay phase-aligned and a
    linear ramp crosses without a dip in the middle.
    """
    mask = np.zeros(length, dtype="float32")
    ramp_length = max(1, int(CROSSFADE * rate))
    ramp = np.linspace(0.0, 1.0, ramp_length, dtype="float32")

    for span in geometry:
        head, tail = span["replace_head"], span["replace_tail"]
        if tail - head < 2:
            continue
        window = np.ones(tail - head, dtype="float32")
        edge = min(ramp_length, (tail - head) // 2)
        if edge > 0:
            window[:edge] = ramp[:edge]
            window[-edge:] = ramp[:edge][::-1]
        # Overlapping lines must not sum past one, so the strongest claim wins.
        mask[head:tail] = np.maximum(mask[head:tail], window)

    return mask


def report_voice_drift(dub, rate, placements, bank):
    """Check that each cloned line still sounds like its character.

    This reads the finished dub track at the position each line was placed,
    not the clips as they came out of the model. Measuring the clips instead
    hid a resampling fault that dropped every compressed line an octave: the
    synthesis was right and the track was wrong, and a check that never looked
    at the track reported all clear.
    """
    import librosa

    heard, blended = {}, 0
    for span in placements:
        speaker, head, tail = span["speaker"], span["head"], span["tail"]
        # A unison line is two characters at once, so its pitch belongs to
        # neither of their references and comparing it to one would report a
        # drift that is really just the other voice.
        if speaker not in bank:
            blended += 1
            continue
        f0, voiced, _ = librosa.pyin(dub[head:tail].mean(axis=1), sr=rate, fmin=60, fmax=500,
                                     frame_length=2048)
        pitches = f0[voiced & ~np.isnan(f0)]
        if pitches.size:
            heard.setdefault(speaker, []).append(float(np.median(pitches)))

    print(f"\n{'CHARACTER':<16}{'ref':>7}{'dub':>7}{'drift':>8}   lines")
    print("-" * 52)
    for speaker, pitches in sorted(heard.items(), key=lambda item: -np.median(item[1])):
        reference = bank[speaker]["pitch"]
        spoken = float(np.median(pitches))
        drift = (spoken - reference) / reference * 100 if reference else 0.0
        flag = "  <- drifted" if abs(drift) > 20 else ""
        print(f"{speaker:<16}{reference:>6}Hz{spoken:>6.0f}Hz{drift:>7.0f}%"
              f"{len(pitches):>8}{flag}")
    if blended:
        print(f"{blended} unison lines not compared: two voices at once have no "
              f"single reference")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("utterances")
    parser.add_argument("voices", help="voice bank directory from dub_voices.py")
    parser.add_argument("stems", help="Demucs output directory holding the two stems")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--video", help="source video, to mux the dub back into")
    parser.add_argument("--from", dest="start", help="start timecode, e.g. 20:38")
    parser.add_argument("--to", dest="end", help="end timecode")
    parser.add_argument("--emo-from-text", action="store_true",
                        help="let the model read emotion out of the line")
    parser.add_argument("--fp16", action="store_true",
                        help="bf16 inference: lighter on the card, and it changes "
                             "what the model produces, so not mid-season")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2_5",
                        help="IndexTTS-2.5 checkpoint directory")
    parser.add_argument("--no-understudies", action="store_true",
                        help="leave characters with no clean audio in the original language")
    parser.add_argument("--compress", type=float, default=COMPRESS_RATIO, metavar="RATIO",
                        help="how hard to even out the dialogue bus "
                             f"(default {COMPRESS_RATIO}, 1 turns it off)")
    parser.add_argument("--attempts", type=int, default=RESYNTH_ATTEMPTS, metavar="N",
                        help="draws allowed for a line that will not fit "
                             f"(default {RESYNTH_ATTEMPTS}, 1 accepts the first)")
    parser.add_argument("--adaptations", metavar="JSON",
                        help="lines rewritten to fit (default beside the utterances)")
    parser.add_argument("--no-adaptations", action="store_true",
                        help="speak the subtitle text as written, however long")
    parser.add_argument("--tuning", metavar="JSON",
                        help="per-character nudges (default voices/tuning.json)")
    parser.add_argument("--tune", action="append", metavar="NAME=key:value,...",
                        help="nudge one character without editing the file, e.g. "
                             "--tune 'PANDA=air:0,pitch:-0.5,gain:1.1'")
    parser.add_argument("--air", type=float, default=AIR_AMOUNT, metavar="RATIO",
                        help="rebuild the octave above the voice model's 11 kHz ceiling "
                             f"(default {AIR_AMOUNT}, 0 turns it off)")
    parser.add_argument("--leak", type=float, default=LEAK_GAIN, metavar="GAIN",
                        help="how loud the original performance sits under the dub "
                             f"(default {LEAK_GAIN}, 0 turns it off)")
    parser.add_argument("--overdubs", metavar="JSON",
                        help="resolved stereo positions from scripts/dub_overdub.py "
                             "(default beside the utterances)")
    parser.add_argument("--no-overdubs", action="store_true",
                        help="render every line centred, even inside a resolved case")
    parser.add_argument("--mix", choices=("replace", "voiceover"),
                        help="replace the original voices with the dub, or duck the "
                             "original and read over it (default: voiceover for a "
                             "solo read, replace for a cast)")
    parser.add_argument("--duck", type=float, default=DUCK_DB, metavar="DB",
                        help=f"how far the original comes down under a voice-over "
                             f"(default {DUCK_DB})")
    parser.add_argument("--shades", metavar="JSON",
                        help="per-role colouring for a solo read "
                             "(default voices/shades.json)")
    parser.add_argument("--subtitles", metavar="PATH",
                        help="the subtitle file the dub was built from, muxed into the "
                             "output beside it (default: the video's own track)")
    parser.add_argument("--no-sequential", action="store_true",
                        help="place every line at its own subtitle, even where one "
                             "voice would then talk over itself")
    parser.add_argument("--clips", metavar="DIR",
                        help="keep each drawn line here, and reuse what is "
                             "already in it; this is what makes a stopped "
                             "render resume rather than start over")
    parser.add_argument("--keep-clips", action="store_true",
                        help="leave the clip directory behind after a "
                             "successful render instead of clearing it")
    parser.add_argument("--pause-file", metavar="PATH",
                        help="stop between lines while this file exists")
    parser.add_argument("--lexicon", metavar="JSON",
                        help="how to spell words the model says wrong, for the "
                             "model only (default <voices>/lexicon.json)")
    parser.add_argument("--sequential", action="store_true",
                        help="queue colliding lines instead of stacking them")
    args = parser.parse_args()

    # Installed before the model is loaded, which is half a minute on its own,
    # so a stop asked during the load is still honoured at the first line.
    pause_file = watch_for_stop(args.pause_file)
    clips = Path(args.clips) if args.clips else None
    if clips:
        clips.mkdir(parents=True, exist_ok=True)

    start, end = parse_timecode(args.start), parse_timecode(args.end)
    episode = json.loads(Path(args.utterances).read_text())
    utterances = [u for u in episode
                  if (start is None or u["start"] >= start)
                  and (end is None or u["end"] <= end)]
    bank = json.loads(Path(args.voices, "bank.json").read_text())
    if not utterances:
        raise SystemExit("no utterances in that range")

    # A solo read says so in the utterance list, and it wants the other set of
    # defaults throughout: read over a ducked original rather than replacing
    # separated voices, and queued rather than stacked, because the one thing
    # one actor cannot do is talk over themselves.
    solo = "role" in utterances[0]
    mix = args.mix or ("voiceover" if solo else "replace")
    sequential = (solo or args.sequential) and not args.no_sequential
    lag = VOICEOVER_LAG if mix == "voiceover" else 0.0

    # A character with no clean audio of their own still gets dubbed, in the
    # closest voice the bank holds. A single original-language line dropped
    # into a dubbed scene is more jarring than an approximate voice.
    # Lines rewritten to fit. A subtitle is written to be read in the time it
    # is on screen; spoken, the same words often cannot be said that fast by
    # anyone, and the answer is fewer words rather than a harder squeeze.
    adaptation_path = Path(args.adaptations) if args.adaptations else Path(
        str(args.utterances).replace(".utterances.json", ".adaptations.json"))
    if adaptation_path.exists() and not args.no_adaptations:
        rewrites = json.loads(adaptation_path.read_text())
        applied, stale = 0, 0
        for utterance in utterances:
            entry = rewrites.get(str(utterance["id"]))
            if not entry or not entry.get("adapted"):
                continue
            # Ids are positions in the utterance list, so re-parsing a subtitle
            # track moves them. The stored original is what proves this rewrite
            # still belongs to this line; without the check a changed parse
            # silently puts one character's words in another's mouth.
            if entry.get("original") not in (None, utterance["text"]):
                stale += 1
                continue
            utterance["text"] = entry["adapted"]
            applied += 1

        if applied:
            print(f"speaking {applied} rewritten lines in place of the subtitle text")
        if stale:
            print(f"IGNORED {stale} rewrites that no longer match their line; "
                  f"re-run dub_adapt.py to carry them over")

    # Where two characters' own lines collide in time, scripts/dub_overdub.py
    # records a stereo position once a case has been reviewed and resolved.
    # A case still sitting at "proposed" is left centred like everything
    # else, and is named here so it keeps surfacing until someone resolves it.
    overdubs_path = Path(args.overdubs) if args.overdubs else Path(
        str(args.utterances).replace(".utterances.json", ".overdubs.json"))
    pan_for = {}
    if overdubs_path.exists() and not args.no_overdubs:
        rendered_ids = {utterance["id"] for utterance in utterances}
        cases = json.loads(overdubs_path.read_text())
        placed, unresolved = 0, []
        for case_id, entry in cases.items():
            if not (set(entry["utterances"]) & rendered_ids):
                continue                    # this case falls outside what is being rendered
            if entry["status"] != "resolved":
                unresolved.append((case_id, entry))
                continue
            for utterance in utterances:
                if utterance["id"] in entry["utterances"]:
                    placement = entry["resolved"].get(utterance["speaker"])
                    if placement:
                        pan_for[utterance["id"]] = placement
                        placed += 1
        if placed:
            print(f"placing {placed} line(s) off-centre from {overdubs_path}")
        for case_id, entry in unresolved:
            print(f"  overdub case {case_id} ({', '.join(entry['speakers'])}) at "
                  f"{entry['span'][0]:.1f}-{entry['span'][1]:.1f}s is not resolved yet, "
                  f"rendered centred - see {entry['image']}")

    # Per-character adjustments, kept beside the bank so they survive a
    # re-render and can be built up by ear over a season.
    tuning_path = Path(args.tuning) if args.tuning else Path(args.voices, "tuning.json")
    tuning = json.loads(tuning_path.read_text()) if tuning_path.exists() else {}
    for override in args.tune or []:
        speaker, _, settings = override.partition("=")
        entry = tuning.setdefault(speaker.strip().upper(), {})
        for pair in settings.split(","):
            key, _, value = pair.partition(":")
            if key.strip():
                entry[key.strip()] = float(value)
    if tuning:
        print("nudged: " + "; ".join(f"{name} {values}" for name, values in tuning.items()))

    # Per-role colouring for a solo read. Anything not named here is coloured
    # from its own name, which keeps a role consistent across a season without
    # anybody having to write it down first.
    shades_path = Path(args.shades) if args.shades else Path(args.voices, "shades.json")
    overrides = json.loads(shades_path.read_text()) if shades_path.exists() else {}
    roles = sorted({utterance["role"] for utterance in utterances
                    if utterance.get("role")})

    # Accents are folded for every line regardless; this is only for words the
    # fold leaves the model still saying wrong. Kept beside the bank, because
    # which words those are is a property of the show being dubbed.
    lexicon_path = Path(args.lexicon) if args.lexicon else Path(args.voices, "lexicon.json")
    say_as = {}
    if lexicon_path.exists():
        say_as = {word.lower(): spoken.lower()
                  for word, spoken in json.loads(lexicon_path.read_text()).items()}
        print("said as: " + "; ".join(f"{w} -> {s}" for w, s in say_as.items()))

    understudy_path = Path(args.voices, "understudies.json")
    if understudy_path.exists() and not args.no_understudies:
        for speaker, entry in json.loads(understudy_path.read_text()).items():
            if entry["voice"] in bank:
                bank[speaker] = {**bank[entry["voice"]], "understudy": entry["voice"]}

    bed, rate = sf.read(Path(args.stems, "no_vocals.wav"), dtype="float32", always_2d=True)
    voices, voice_rate = sf.read(Path(args.stems, "vocals.wav"), dtype="float32", always_2d=True)
    if voice_rate != rate or voices.shape != bed.shape:
        raise SystemExit("the two stems do not line up")

    # Which way each line leans, measured off what the original voice was
    # doing in that exact span. Nothing here needs to know who is speaking.
    # Measured across the whole episode, not just the span being rendered. The
    # neutral is the register the show mostly sits in, and a sixty-second
    # preview of one scene does not contain it — judged inside the preview, a
    # scene of one character would make that character the neutral and come
    # out unshaded, so the preview would not show what the episode does.
    shades = {role: {**shade_for(role), **overrides.get(role, {})} for role in roles}
    lifts, role_shades, neutral = ({}, {}, 0.0)
    if solo:
        lifts, role_shades, neutral = line_shades(
            episode, *track_pitch(voices, rate))

    reader = bank.get(SOLO_ACTOR, {})
    if solo and neutral:
        who = reader.get("actor")
        leaning = [lift for lift in lifts.values() if abs(lift) >= SHADE_STEP]
        print(f"\nread by {who}" if who else "\nsolo read")
        print(f"  the episode's own voices sit around {neutral:.0f} Hz; "
              f"{len(lifts)} of its {len(episode)} lines\n  were measurable and "
              f"{len(leaning)} of those lean far enough to hear")
        if role_shades:
            print(f"  by role, where the labelling named one:")
            for role, (shade, pitch) in sorted(role_shades.items(),
                                               key=lambda item: -item[1][1]):
                print(f"    {role:<24}{pitch:>5} Hz  {shade:>+6.2f} st")

    # IndexTTS-2.5. The 2 path is gone rather than kept behind a flag: a season
    # cannot be split across the two anyway — the clone differs, so the whole
    # cast changes voice mid-run — which makes the flag a way to ruin a season
    # rather than a way to choose. Anything rendered on 2 is being rendered
    # again, and git holds the old path if that ever stops being true.
    checkpoints = Path(args.checkpoints)
    from indextts.infer_v2_5 import IndexTTS2
    tts = IndexTTS2(cfg_path=str(checkpoints / "config.yaml"),
                    model_dir=str(checkpoints), use_bf16=args.fp16)

    # 2.5 wants the language named rather than guessed, and every line here is
    # English by construction: the dub is the translation.
    def speak(**kwargs):
        return tts.infer(lang="EN", **kwargs)

    print(f"speaking with IndexTTS-2.5 from {checkpoints}")

    workdir = Path(tempfile.mkdtemp())
    print(f"speaking {len(utterances)} lines")
    try:
        rendered = synthesize(speak, utterances, bank, workdir, args.emo_from_text,
                              args.attempts, clips, pause_file, say_as)
    except Paused:
        held = len(list(clips.glob("*.wav"))) if clips else 0
        print(f"\npaused with {held} of {len(utterances)} lines drawn; nothing "
              f"was written to {args.output}" if clips else
              "\npaused; without --clips the lines drawn so far are lost")
        return EXIT_PAUSED
    if not rendered:
        raise SystemExit("nothing was synthesized")

    dub, report, geometry = build_track(rendered, bed, voices, rate, args.air, tuning,
                                        pan_for, shades, sequential, lag, lifts)

    # Demucs splits the mix into exactly two parts, so summing them gives the
    # original back without re-reading the video.
    dub = compress_dialogue(dub, rate, args.compress)
    original = bed + voices

    exported = (int((start or 0) * rate),
                int(end * rate) if end is not None else original.shape[0])
    if mix == "voiceover":
        ducked, share = duck_original(original, dub, rate, args.duck, exported)
        mixed = ducked + dub * DUB_GAIN
        print(f"\nread over the original, ducked {args.duck:.0f} dB across the "
              f"{share * 100:.0f}% of the exported audio\nthe voice-over speaks; the "
              f"rest plays at full level, and the stems are summed\nback rather than "
              f"used apart, so no separation artefact survives")
    else:
        mask = replacement_mask(geometry, bed.shape[0], rate)[:, None]
        replaced = bed * BED_GAIN + dub * DUB_GAIN

        # Only inside the replaced spans. Everywhere else the original is
        # already playing at full level, and adding a copy of it to itself
        # would only comb-filter the parts of the episode never touched.
        if args.leak > 0:
            replaced = replaced + duck_centre(original, rate) * args.leak
        mixed = original * (1.0 - mask) + replaced * mask

    peak = np.abs(mixed).max()
    if peak > 0.99:
        mixed = mixed * (0.99 / peak)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Built under a name of its own and moved onto the finished one only when
    # it is complete, so a render killed inside ffmpeg cannot leave a truncated
    # episode for Emby to index. Beside the finished name, so the move is a
    # rename within one directory and cannot half-happen. The leading dot keeps
    # the unfinished file out of the library's way meanwhile, and the extension
    # stays on the end because both ffmpeg and soundfile choose what to write
    # by looking at it — named `.partial`, ffmpeg cannot tell it is matroska.
    staged = output.with_name(f".{output.stem}.partial{output.suffix}")
    staged.unlink(missing_ok=True)

    full_mix = workdir / "dub.wav"
    sf.write(full_mix, mixed, rate)

    if args.video:
        clip = ["-ss", str(start)] if start is not None else []
        span = ["-t", str(end - (start or 0))] if end is not None else []

        # A dub is a translation spoken by a machine, so the words it was
        # working from travel with it. Two tracks, because they answer
        # different questions: the source says what the scene means, and the
        # spoken track says what the dub actually just said, which is the one
        # you want the moment a line comes out wrong.
        # Subtitles are shifted here rather than with ffmpeg's -ss. Input
        # seeking a text subtitle lands on a cue boundary rather than on the
        # time asked for, which put the source track ten seconds out on a
        # preview and left a cue from before the export sitting at zero.
        # Rewriting the timestamps is exact and needs no seeking at all.
        spoken = workdir / "spoken.srt"
        lines = write_spoken_subtitles(report, utterances, spoken, start or 0.0)

        source = subtitle_source(args, workdir)
        if source is not None and (start or end):
            shifted = workdir / f"source{source.suffix}"
            shift_file(source, shifted, -(start or 0.0),
                       window=(0.0, (end - (start or 0.0)) if end else float("inf")))
            source = shifted

        # Inputs in a fixed order so the stream indices below are readable:
        # the video, the finished mix, the source subtitles where there are
        # any, and the track of what was actually spoken.
        sources = ([source] if source is not None else []) + [spoken]
        inputs = [argument for path in sources for argument in ("-i", str(path))]
        maps = [argument for position in range(len(sources))
                for argument in ("-map", f"{2 + position}:s:0")]

        titles = ["English (the dub's source)", "English (as spoken)"][-len(sources):]
        tags = [argument for position, title in enumerate(titles)
                for argument in (f"-metadata:s:s:{position}", f"title={title}",
                                 f"-metadata:s:s:{position}", "language=eng")]

        subprocess.run(["ffmpeg", "-v", "error",
                        *clip, "-i", str(args.video),
                        *clip, "-i", str(full_mix),
                        *inputs,
                        "-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:0", *maps,
                        # Copied rather than converted, so a fansub's own
                        # typesetting arrives as the fansub drew it instead of
                        # as SRT's font tags.
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "copy",
                        "-metadata:s:a:0", "title=English (AI dub)",
                        "-metadata:s:a:0", "language=eng",
                        "-metadata:s:a:1", "title=Japanese",
                        # The original carries its own default flag out of the
                        # source, and setting one here does not clear the other:
                        # both tracks then claim to be the default and which
                        # language a player picks is its own business. Cleared
                        # explicitly, so the dub is the default and the original
                        # is the one you choose.
                        "-disposition:a:0", "default",
                        "-disposition:a:1", "0",
                        *tags, "-disposition:s:0", "default",
                        *span, str(staged), "-y"], check=True)
        print(f"\nmuxed {len(sources)} subtitle track(s): "
              + ("the source the dub was built from, and " if source is not None
                 else "no source track was found, only ")
              + f"the {lines} lines it spoke, at the times it spoke them")
    else:
        sf.write(staged, mixed, rate)

    report_voice_drift(dub, rate, geometry, bank)

    # Measured over the span actually exported. Against the whole episode the
    # number would just report how little of it this scene covers.
    if mix == "replace":
        print(f"\n{float(mask[slice(*exported)].mean()) * 100:.0f}% of the exported "
              f"audio uses the separated bed; the rest plays the original untouched")

    if sequential:
        queued = [row for row in report if row["late"] > lag + 0.05]
        behind = max((row["late"] for row in report), default=0.0)
        print(f"\n{len(queued)} lines waited for the previous one to finish, the "
              f"latest by {behind:.1f}s")
        for row in sorted(queued, key=lambda r: -r["late"])[:5]:
            print(f"  line {row['id']} {row['late']:.1f}s late")

    gains = sorted(row["gain"] for row in report)
    if gains:
        quiet, loud = gains[0], gains[-1]
        print(f"level matched per line: {20 * np.log10(quiet):+.1f} dB to "
              f"{20 * np.log10(loud):+.1f} dB against what the model produced, "
              f"a {20 * np.log10(loud / quiet):.0f} dB spread across the scene")

    # Stamped on every row because the report is a list rather than a document
    # with a header, and dub_inspect.py has to know which mix it is looking at:
    # a bed that moves under the dialogue is a fault in one mode and the whole
    # design in the other.
    for row in report:
        row["mix"] = mix

    compressed = [row for row in report if row["compression"] > 1.01]
    overflowed = [row for row in report if row["overflow"] > 0.25]
    print(f"\n{len(report)} lines placed, {len(compressed)} compressed to fit, "
          f"{len(overflowed)} still overrunning")
    for row in sorted(overflowed, key=lambda r: -r["overflow"])[:5]:
        print(f"  line {row['id']} {row['speaker']}: {row['overflow']}s over")

    # The timing lands first and the episode second, so the moment the episode
    # exists under its finished name the report that describes it is already
    # there. Nothing downstream ever sees one without the other.
    Path(str(staged) + ".timing.json").write_text(json.dumps(report, indent=1))
    Path(str(staged) + ".timing.json").replace(str(output) + ".timing.json")
    staged.replace(output)
    print(f"wrote {output}")

    # Only now, with the episode finished on disk, are the clips spent — for a
    # preview. A season keeps them (`--keep-clips`, which dub_season passes),
    # because they are also what makes repairing one line of a finished episode
    # cost that line rather than the quarter hour behind it. They run about
    # 120 KB a line, so an episode is some 40 MB and a season a couple of GB.
    if clips and not args.keep_clips:
        shutil.rmtree(clips, ignore_errors=True)

    # Asked to stop while the mix was already running. The episode was worth
    # finishing — it was minutes of CPU from done, and the GPU was idle — but
    # the caller still needs to hear that a stop was asked for.
    if stop_asked(pause_file):
        print("stop was asked for during the mix; this episode finished, "
              "nothing further will start")
        return EXIT_PAUSED

    return 0


if __name__ == "__main__":
    sys.exit(main())
