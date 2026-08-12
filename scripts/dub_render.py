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
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

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

# Screen dialogue is mixed to the centre, so the centre is ducked across the
# band a voice occupies. Outside that band, and anywhere off centre, the score
# and the effects are left alone.
LEAK_BAND = (300.0, 3400.0)

# What survives of the centre inside that band. Not zero: cancelling the centre
# outright also takes the sound design sitting behind the actor, and the point
# is to hear the performance quietly, not to remove it twice.
LEAK_CENTRE = 0.3


def parse_timecode(value):
    if value is None:
        return None
    parts = str(value).split(":")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def fit_to_slot(clip_path, generated, available, rate, semitones=0.0):
    """Compress a generated line only as far as it stays natural.

    The returned audio is always at `rate`, whether or not it was compressed.
    Resampling has to happen in exactly one place: when the caller resampled a
    second time on the compression path, every compressed line played an octave
    low and at twice the length, running over the next speaker.
    """
    factor = 1.0
    if available > 0.2 and generated > available:
        factor = min(generated / available, MAX_COMPRESSION)

    settings = []
    if factor > 1.0:
        settings.append(f"tempo={factor:.4f}")
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


def synthesize(tts, utterances, bank, workdir, emo_from_text):
    """Generate one wav per utterance, in that character's cloned voice."""
    rendered = []
    for utterance in utterances:
        if utterance["group"]:
            continue                       # a crowd: left in the original audio
        voice = bank.get(utterance["speaker"])
        if voice is None:
            print(f"  no voice for {utterance['speaker']}, left in the original language")
            continue

        output = workdir / f"{utterance['id']:04d}.wav"
        kwargs = {"use_emo_text": True, "emo_alpha": 0.8} if emo_from_text else {}
        # Asking for no output path returns the audio instead of writing it.
        # Writing it here keeps the pipeline off torchaudio.save, which needs
        # TorchCodec on current torchaudio and fails without it.
        result = tts.infer(spk_audio_prompt=voice["path"], text=utterance["text"],
                           output_path=None, verbose=False, **kwargs)
        if result is None:
            print(f"  synthesis produced nothing for line {utterance['id']}")
            continue
        sample_rate, samples = result
        sf.write(output, np.asarray(samples).astype("float32") / 32768.0, sample_rate)
        rendered.append((utterance, output))
        print(f"  [{utterance['id']:>3}] {utterance['speaker']:<12} {utterance['text'][:52]}")

    return rendered


def speech_level(samples, rate, floor=0.15):
    """Loudness of the speech in a clip, ignoring the silence around it.

    Plain RMS over a whole line rates a short word in a long subtitle window
    as quiet, because it averages in the pauses. Only the frames carrying
    speech are counted, so the number describes the delivery rather than the
    timing.
    """
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

    evened, produced = sf.read(workdir / "post.wav", dtype="float32")
    if produced != rate:
        raise SystemExit(f"compressor produced {produced} Hz, expected {rate}")

    after = speech_level(evened, rate)
    if after > 0:
        evened = evened * (before / after)
    return evened[:dub.size].astype("float32")


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


def build_track(rendered, bed, voices, rate, air, tuning):
    """Lay each spoken line onto a silent track at its scene position.

    The bed stays stereo because it carries the score and the sound design,
    and folding it to mono to match the voices would throw away the stereo
    image of the original mix. The generated speech is mono, so it goes to
    the centre, which is where screen dialogue belongs anyway.
    """
    dub = np.zeros(bed.shape[0], dtype="float32")
    report, geometry, lines = [], [], []

    for utterance, clip_path in rendered:
        probe = sf.info(str(clip_path))
        generated = probe.frames / probe.samplerate
        available = utterance["window"] + max(0.0, utterance["slack"] - SPEAKER_MARGIN)

        # Per-character nudges. Characters do not all take the same treatment:
        # the brighter a voice, the more the air stage colours it, and a high
        # voice given the default reads as though it went through a vocoder.
        nudge = tuning.get(utterance["speaker"], {})
        audio, factor = fit_to_slot(clip_path, generated, available, rate,
                                    nudge.get("pitch", 0.0))
        spoken = voices[int(utterance["start"] * rate):int(utterance["end"] * rate)]
        produced = speech_level(audio, rate)
        lines.append({"utterance": utterance, "audio": audio, "factor": factor,
                      "generated": generated, "available": available,
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
        gain *= tuning.get(utterance["speaker"], {}).get("gain", 1.0)

        head = int(utterance["start"] * rate)
        tail = min(head + audio.size, dub.size)
        dub[head:tail] += audio[:tail - head] * gain

        # The span to take over from the original covers both the line as
        # spoken in Japanese and the dub that replaces it, whichever runs
        # longer, so no part of the original voice is left ringing underneath.
        pad = int(REPLACE_PAD * rate)
        geometry.append({
            "speaker": utterance["speaker"], "head": head, "tail": tail,
            "replace_head": max(0, head - pad),
            "replace_tail": min(dub.size, max(tail, int(utterance["end"] * rate)) + pad)})

        report.append({"id": utterance["id"], "speaker": utterance["speaker"],
                       "generated": round(line["generated"], 2),
                       "available": round(line["available"], 2),
                       "compression": round(line["factor"], 3),
                       "gain": round(float(gain), 3),
                       "overflow": round(max(0.0, line["generated"] / line["factor"]
                                             - line["available"]), 2)})

    return dub, report, geometry


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

    heard = {}
    for span in placements:
        speaker, head, tail = span["speaker"], span["head"], span["tail"]
        f0, voiced, _ = librosa.pyin(dub[head:tail], sr=rate, fmin=60, fmax=500,
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
    parser.add_argument("--fp16", action="store_true", help="half precision inference")
    parser.add_argument("--checkpoints", default="dub/checkpoints_2",
                        help="IndexTTS-2 checkpoint directory")
    parser.add_argument("--no-understudies", action="store_true",
                        help="leave characters with no clean audio in the original language")
    parser.add_argument("--compress", type=float, default=COMPRESS_RATIO, metavar="RATIO",
                        help="how hard to even out the dialogue bus "
                             f"(default {COMPRESS_RATIO}, 1 turns it off)")
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
    args = parser.parse_args()

    start, end = parse_timecode(args.start), parse_timecode(args.end)
    utterances = [u for u in json.loads(Path(args.utterances).read_text())
                  if (start is None or u["start"] >= start)
                  and (end is None or u["end"] <= end)]
    bank = json.loads(Path(args.voices, "bank.json").read_text())
    if not utterances:
        raise SystemExit("no utterances in that range")

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

    understudy_path = Path(args.voices, "understudies.json")
    if understudy_path.exists() and not args.no_understudies:
        for speaker, entry in json.loads(understudy_path.read_text()).items():
            if entry["voice"] in bank:
                bank[speaker] = {**bank[entry["voice"]], "understudy": entry["voice"]}

    bed, rate = sf.read(Path(args.stems, "no_vocals.wav"), dtype="float32", always_2d=True)
    voices, voice_rate = sf.read(Path(args.stems, "vocals.wav"), dtype="float32", always_2d=True)
    if voice_rate != rate or voices.shape != bed.shape:
        raise SystemExit("the two stems do not line up")

    from indextts.infer_v2 import IndexTTS2
    checkpoints = Path(args.checkpoints)
    tts = IndexTTS2(cfg_path=str(checkpoints / "config.yaml"), model_dir=str(checkpoints),
                    use_fp16=args.fp16)

    workdir = Path(tempfile.mkdtemp())
    print(f"speaking {len(utterances)} lines")
    rendered = synthesize(tts, utterances, bank, workdir, args.emo_from_text)
    if not rendered:
        raise SystemExit("nothing was synthesized")

    dub, report, geometry = build_track(rendered, bed, voices, rate, args.air, tuning)

    # Demucs splits the mix into exactly two parts, so summing them gives the
    # original back without re-reading the video.
    dub = compress_dialogue(dub, rate, args.compress)

    original = bed + voices
    mask = replacement_mask(geometry, bed.shape[0], rate)[:, None]
    replaced = bed * BED_GAIN + dub[:, None] * DUB_GAIN

    # Only inside the replaced spans. Everywhere else the original is already
    # playing at full level, and adding a copy of it to itself would only
    # comb-filter the parts of the episode that were never touched.
    if args.leak > 0:
        replaced = replaced + duck_centre(original, rate) * args.leak

    mixed = original * (1.0 - mask) + replaced * mask

    peak = np.abs(mixed).max()
    if peak > 0.99:
        mixed = mixed * (0.99 / peak)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    full_mix = workdir / "dub.wav"
    sf.write(full_mix, mixed, rate)

    if args.video:
        clip = ["-ss", str(start)] if start is not None else []
        span = ["-t", str(end - (start or 0))] if end is not None else []
        subprocess.run(["ffmpeg", "-v", "error",
                        *clip, "-i", str(args.video),
                        *clip, "-i", str(full_mix), *span,
                        "-map", "0:v:0", "-map", "1:a:0", "-map", "0:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-metadata:s:a:0", "title=English (AI dub)",
                        "-metadata:s:a:0", "language=eng",
                        "-metadata:s:a:1", "title=Japanese",
                        "-disposition:a:0", "default",
                        str(output), "-y"], check=True)
    else:
        sf.write(output, mixed, rate)

    report_voice_drift(dub, rate, geometry, bank)

    # Measured over the span actually exported. Against the whole episode the
    # number would just report how little of it this scene covers.
    span = slice(int((start or 0) * rate),
                 int(end * rate) if end is not None else mask.shape[0])
    print(f"\n{float(mask[span].mean()) * 100:.0f}% of the exported audio uses the "
          f"separated bed; the rest plays the original untouched")

    gains = sorted(row["gain"] for row in report)
    if gains:
        quiet, loud = gains[0], gains[-1]
        print(f"level matched per line: {20 * np.log10(quiet):+.1f} dB to "
              f"{20 * np.log10(loud):+.1f} dB against what the model produced, "
              f"a {20 * np.log10(loud / quiet):.0f} dB spread across the scene")

    compressed = [row for row in report if row["compression"] > 1.01]
    overflowed = [row for row in report if row["overflow"] > 0.25]
    print(f"\n{len(report)} lines placed, {len(compressed)} compressed to fit, "
          f"{len(overflowed)} still overrunning")
    for row in sorted(overflowed, key=lambda r: -r["overflow"])[:5]:
        print(f"  line {row['id']} {row['speaker']}: {row['overflow']}s over")
    Path(str(output) + ".timing.json").write_text(json.dumps(report, indent=1))
    print(f"wrote {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
