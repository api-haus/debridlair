#!/usr/bin/env python3
"""Cut a voice bank: one reference clip per character, for voice cloning.

A cloned voice is only as good as the audio it is cloned from. Anime dialogue
sits on top of music and effects, so a clip chosen by subtitle timing alone
often carries a music bed, a sound effect, or another character talking, and
the clone inherits all of it.

This tool scores every candidate clip against both stems. A clip is good when
the vocals stem is loud and the accompaniment stem is quiet at the same
moment, which means the character is speaking with little behind them. The
best few clips per character are trimmed to their speech and joined into one
reference file.

Requires the Demucs stems and the utterance list:
    demucs --two-stems=vocals -o stems EPISODE.wav
    python3 scripts/dub_script.py EPISODE.mkv -o utterances.json

Usage:
    python3 scripts/dub_voices.py utterances.json stems/htdemucs/e01.audio -o voices/
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# Cloning wants a handful of seconds of speech. Below the floor there is not
# enough timbre to copy; past the ceiling the model gains nothing.
MIN_CLIP, MAX_CLIP = 1.2, 11.0
TARGET_REFERENCE = 10.0

# A clip is rejected when the music behind it is within this ratio of the
# voice, because the clone would learn the music as part of the voice. Set
# from measurement: at a ratio near 4 the pitch of the reference tracks the
# music rather than the character, which showed up as Panda's mother reading
# as a baritone. Clips at 10 and above hold their character's real pitch.
MIN_VOICE_RATIO = 10.0

# Silence trimmed from each end, relative to the clip's own peak.
TRIM_FLOOR = 0.06

JOIN_SILENCE = 0.15


def load_stems(stem_dir):
    stem_dir = Path(stem_dir)
    vocals, rate = sf.read(stem_dir / "vocals.wav", dtype="float32", always_2d=True)
    music, music_rate = sf.read(stem_dir / "no_vocals.wav", dtype="float32", always_2d=True)
    if rate != music_rate:
        raise SystemExit("the two stems disagree on sample rate")
    return vocals.mean(axis=1), music.mean(axis=1), rate


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0


def trim_silence(clip, rate):
    """Cut leading and trailing silence so the reference is speech throughout."""
    if clip.size == 0:
        return clip
    window = max(1, int(0.02 * rate))
    envelope = np.abs(clip)
    smoothed = np.convolve(envelope, np.ones(window) / window, mode="same")
    loud = np.flatnonzero(smoothed > TRIM_FLOOR * smoothed.max())
    if loud.size == 0:
        return clip
    pad = int(0.05 * rate)
    return clip[max(0, loud[0] - pad):min(clip.size, loud[-1] + pad)]


FRAME = 0.05

# Speech dips at every consonant and breath, so the loudness either side of a
# frame decides whether it is clean, not the frame alone. Judged frame by frame
# a continuous sentence shatters into fragments too short to clone from.
SMOOTH_FRAMES = 5

# A gap this short inside otherwise clean speech is part of the delivery, not
# a break in it, so runs either side are joined rather than kept apart.
BRIDGE_FRAMES = 4


def framewise_levels(vocals, music, rate):
    """Voice and music loudness per short frame, across the whole episode."""
    width = int(FRAME * rate)
    usable = (vocals.size // width) * width
    voice = np.sqrt(np.mean(np.square(vocals[:usable].reshape(-1, width)), axis=1))
    behind = np.sqrt(np.mean(np.square(music[:usable].reshape(-1, width)), axis=1))

    window = np.ones(SMOOTH_FRAMES) / SMOOTH_FRAMES
    voice = np.convolve(voice, window, mode="same")
    behind = np.convolve(behind, window, mode="same")
    return voice, behind, width


def bridge_gaps(mask, span):
    """Close gaps of up to `span` frames so one sentence stays one run."""
    closed = mask.copy()
    edges = np.flatnonzero(mask)
    for left, right in zip(edges, edges[1:]):
        if 1 < right - left <= span + 1:
            closed[left:right] = True
    return closed


def score_clips(utterances, vocals, music, rate):
    """Find the clean stretches of speech inside each character's lines.

    Scoring a whole subtitle line rejects the entire line as soon as music
    comes in under any part of it, which threw away characters who only ever
    speak over a scene's score. Most such lines still hold a second or two of
    the character in the clear. This looks frame by frame within each line and
    keeps those stretches, which is what gets a supporting cast banked at all.
    """
    voice, behind, width = framewise_levels(vocals, music, rate)
    spans = [(u["start"], u["end"], u["speaker"]) for u in utterances]
    clean = bridge_gaps((voice > 0.005) & (voice / (behind + 1e-6) >= MIN_VOICE_RATIO),
                        BRIDGE_FRAMES)
    candidates = []

    for utterance in utterances:
        if utterance["group"]:
            continue
        start, end = utterance["start"], utterance["end"]
        # Another character talking over the line poisons the timbre.
        if any(start < other_end and other_start < end and speaker != utterance["speaker"]
               for other_start, other_end, speaker in spans):
            continue

        first, last = int(start * rate) // width, min(int(end * rate) // width, clean.size)
        run = first
        while run < last:
            if not clean[run]:
                run += 1
                continue
            stop = run
            while stop < last and clean[stop]:
                stop += 1

            duration = (stop - run) * FRAME
            if MIN_CLIP <= duration:
                head, tail = run * width, min(stop * width, vocals.size)
                candidates.append({
                    "speaker": utterance["speaker"], "head": head, "tail": tail,
                    "ratio": float(np.mean(voice[run:stop] / (behind[run:stop] + 1e-6))),
                    "level": float(np.mean(voice[run:stop])),
                    "duration": min(duration, MAX_CLIP), "text": utterance["text"]})
            run = stop

    return candidates


def build_bank(candidates, vocals, rate, output_dir):
    """Join each character's cleanest clips into one reference file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gap = np.zeros(int(JOIN_SILENCE * rate), dtype="float32")

    by_speaker = {}
    for candidate in candidates:
        by_speaker.setdefault(candidate["speaker"], []).append(candidate)

    bank = {}
    for speaker, clips in by_speaker.items():
        clips.sort(key=lambda clip: -clip["ratio"])
        pieces, total, used = [], 0.0, []
        for clip in clips:
            if total >= TARGET_REFERENCE:
                break
            trimmed = trim_silence(vocals[clip["head"]:clip["tail"]], rate)
            if trimmed.size / rate < 0.4:
                continue
            pieces.append(trimmed)
            total += trimmed.size / rate
            used.append(clip)

        if not pieces or total < 1.0:
            continue

        joined = np.concatenate([piece for pair in zip(pieces, [gap] * len(pieces))
                                 for piece in pair][:-1])
        peak = np.abs(joined).max()
        if peak > 0:
            joined = joined * (0.95 / peak)

        name = "".join(char if char.isalnum() else "_" for char in speaker).strip("_")
        path = output_dir / f"{name}.wav"
        sf.write(path, joined, rate)
        bank[speaker] = {"path": str(path), "seconds": round(total, 2),
                         "clips": len(pieces),
                         "ratio": round(sum(c["ratio"] for c in used) / len(used), 1),
                         "pitch": round(median_pitch(joined, rate)),
                         "lines": [c["text"][:50] for c in used]}

    return bank


def cast_understudies(utterances, bank, vocals, music, rate):
    """Give every remaining speaker the closest banked voice.

    Some characters never get a clean reference: a tannoy announcement carries
    processing and room, a one-line walk-on has nothing to spare. Leaving them
    out means their lines keep playing in the original language in the middle
    of a dubbed scene, which is more jarring than an approximate voice. Each is
    matched to the banked voice nearest in pitch, so an announcer is read by an
    announcer-shaped voice rather than by whoever happens to be first.
    """
    banked = {name: entry["pitch"] for name, entry in bank.items() if entry["pitch"]}
    if not banked:
        return {}

    # These speakers failed the cloning bar, so their audio carries music. Take
    # the least bad of it rather than all of it: pitch measured over a character's
    # music-heavy frames tracks the music, which cast Panda's mother as a llama.
    voice, behind, width = framewise_levels(vocals, music, rate)
    passable = voice / (behind + 1e-6)

    understudies = {}
    for speaker in {u["speaker"] for u in utterances if not u["group"]} - set(bank):
        spans = [u for u in utterances if u["speaker"] == speaker]
        frames = np.concatenate([np.arange(int(u["start"] * rate) // width,
                                           min(int(u["end"] * rate) // width, voice.size))
                                 for u in spans]) if spans else np.array([], dtype=int)
        frames = frames[voice[frames] > 0.005]
        if not frames.size:
            continue

        keep = frames[passable[frames] >= max(3.0, np.median(passable[frames]))]
        chosen = keep if keep.size else frames
        audio = np.concatenate([vocals[frame * width:(frame + 1) * width]
                                for frame in chosen[:int(20 / FRAME)]])
        estimate = median_pitch(audio, rate) if audio.size else 0.0
        if not estimate:
            continue
        stand_in = min(banked, key=lambda name: abs(banked[name] - estimate))
        understudies[speaker] = {"voice": stand_in, "pitch": round(estimate),
                                 "lines": len(spans)}

    return understudies


def median_pitch(clip, rate):
    """Median voiced pitch, in Hz.

    Two characters whose references share a pitch will clone to voices that
    sound alike, which defeats the point. Reporting pitch turns "the voices are
    distinct" from an assumption into something visible before any GPU time is
    spent.
    """
    import librosa

    f0, voiced, _ = librosa.pyin(clip, sr=rate, fmin=60, fmax=500,
                                 frame_length=2048)
    heard = f0[voiced & ~np.isnan(f0)]
    return float(np.median(heard)) if heard.size else 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", required=True, help="directory for the voice bank")
    parser.add_argument("--episode", nargs=2, action="append", required=True,
                        metavar=("UTTERANCES", "STEMS"),
                        help="an episode's utterance JSON and Demucs stem directory; "
                             "repeat to pool a supporting cast across a season")
    args = parser.parse_args()

    # Pooling matters because a minor character speaks a few clean seconds per
    # episode. Across a season that adds up to a clonable voice, while any one
    # episode gives too little.
    pooled, everyone, voice_parts, music_parts, rate = [], [], [], [], None
    played = 0                      # samples of audio already laid down
    for utterance_path, stem_dir in args.episode:
        utterances = json.loads(Path(utterance_path).read_text())
        episode_vocals, episode_music, rate = load_stems(stem_dir)

        for candidate in score_clips(utterances, episode_vocals, episode_music, rate):
            candidate["head"] += played
            candidate["tail"] += played
            pooled.append(candidate)

        # Each episode's timestamps restart at zero, so they are shifted onto
        # the pooled timeline. Without this every episode after the first reads
        # its speakers out of the wrong part of the audio.
        for utterance in utterances:
            shifted = dict(utterance)
            shifted["start"] += played / rate
            shifted["end"] += played / rate
            everyone.append(shifted)

        voice_parts.append(episode_vocals)
        music_parts.append(episode_music)
        played += episode_vocals.size

    vocals, music = np.concatenate(voice_parts), np.concatenate(music_parts)

    bank = build_bank(pooled, vocals, rate, args.output)
    Path(args.output, "bank.json").write_text(json.dumps(bank, indent=1, ensure_ascii=False))

    understudies = cast_understudies(everyone, bank, vocals, music, rate)
    Path(args.output, "understudies.json").write_text(
        json.dumps(understudies, indent=1, ensure_ascii=False))

    print(f"{'CHARACTER':<16}{'ref':>7}{'clips':>7}{'clean':>8}{'pitch':>8}   cloned from")
    print("-" * 86)
    for speaker, entry in sorted(bank.items(), key=lambda item: -item[1]["pitch"]):
        print(f"{speaker:<16}{entry['seconds']:>6.1f}s{entry['clips']:>7}"
              f"{entry['ratio']:>7.0f}x{entry['pitch']:>7}Hz   {entry['lines'][0][:32]}")

    thin = [speaker for speaker, entry in bank.items() if entry["seconds"] < 3.0]
    print(f"\n{len(bank)} voices banked in {args.output}")
    if thin:
        print(f"thin, pool more episodes: {', '.join(sorted(thin))}")

    if understudies:
        print(f"\nno clean audio, standing in with the nearest voice:")
        for speaker, entry in sorted(understudies.items(), key=lambda i: -i[1]["lines"]):
            print(f"  {speaker:<14}{entry['pitch']:>5}Hz -> {entry['voice']:<12}"
                  f"{entry['lines']:>3} lines")

    return 0


if __name__ == "__main__":
    sys.exit(main())
