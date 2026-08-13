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

`--solo` mints one voice instead of a cast, for a track that names nobody.
There is no attribution to cut clips by, so the clips are cut by pitch: the
voice heard most across the episode is the one the show belongs to, and it is
the one an amateur dub would have been read in.

Usage:
    python3 scripts/dub_voices.py utterances.json stems/htdemucs/e01.audio -o voices/
    python3 scripts/dub_voices.py -o voices/ --solo --episode utterances.json stems/
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_script import SOLO_ACTOR  # noqa: E402

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


def score_clips(utterances, vocals, music, rate, solo=False, role=None):
    """Find the clean stretches of speech inside each character's lines.

    Scoring a whole subtitle line rejects the entire line as soon as music
    comes in under any part of it, which threw away characters who only ever
    speak over a scene's score. Most such lines still hold a second or two of
    the character in the clear. This looks frame by frame within each line and
    keeps those stretches, which is what gets a supporting cast banked at all.

    `role` narrows the search to the lines a solo read's labelling gave to one
    character, which is how a voice gets cut out of a show that names nobody.
    The labelling is a guess and is treated as one: it only decides which
    clips are looked at, and the pitch clustering afterwards throws out
    whatever came back a different voice.
    """
    voice, behind, width = framewise_levels(vocals, music, rate)
    spans = [(u["start"], u["end"], u["speaker"]) for u in utterances]
    clean = bridge_gaps((voice > 0.005) & (voice / (behind + 1e-6) >= MIN_VOICE_RATIO),
                        BRIDGE_FRAMES)
    candidates = []

    for utterance in utterances:
        # A sign is words on the picture. Whatever is being said underneath it
        # is not what the sign says, so its span is not a clip of anybody.
        if utterance["group"] or utterance.get("kind") == "sign":
            continue
        if role is not None and utterance.get("role") != role:
            continue
        start, end = utterance["start"], utterance["end"]
        # Another character talking over the line poisons the timbre. On a solo
        # read every line carries the same speaker name, which is the parser
        # saying it does not know rather than saying they match, so there any
        # overlap at all disqualifies the clip.
        if any(start < other_end and other_start < end
               and (solo or speaker != utterance["speaker"])
               and (other_start, other_end) != (start, end)
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


# How wide a band of pitch counts as one voice, in semitones either side of
# the middle of it. A performance moves around within a scene; two different
# actors sitting this close would clone alike anyway.
SOLO_BAND = 2.0

# Pitching every candidate clip in an episode costs more than it is worth, and
# the cleanest ones are the only ones that can win a place in the reference.
SOLO_CANDIDATES = 90


def mint_solo(candidates, vocals, rate, wanted=None):
    """Cut one reference from the voice heard most across the episode.

    Nothing here knows who is speaking, so the clean clips are a mix of the
    whole cast and joining them would clone an average of everybody. What
    separates them without attribution is pitch: cluster the clips, take the
    band holding the most speech, and the reference is one person again.

    The band that wins is the one on screen most, which on a narrated show is
    the narrator and on any other show is the lead. Either is the right voice
    to hand a solo dub — it is the one the show already sounds like.
    """
    ranked = sorted(candidates, key=lambda clip: -clip["ratio"])[:SOLO_CANDIDATES]
    for clip in ranked:
        clip["pitch"] = median_pitch(trim_silence(vocals[clip["head"]:clip["tail"]], rate),
                                     rate)
    voiced = [clip for clip in ranked if clip["pitch"] > 0]
    if not voiced:
        raise SystemExit("no clip in the episode holds a pitch to cluster on")

    # Semitones, so a band is the same musical width wherever it sits. In Hz a
    # fixed window is wide around a low voice and narrow around a high one.
    import numpy as np
    keys = np.log2(np.asarray([clip["pitch"] for clip in voiced])) * 12.0
    if wanted:
        middle = float(np.log2(wanted) * 12.0)
    else:
        # The densest band, weighted by how much speech each clip carries,
        # since a band of many short clips is not a voice heard more.
        weights = np.asarray([clip["duration"] for clip in voiced])
        totals = [float(weights[np.abs(keys - key) <= SOLO_BAND].sum()) for key in keys]
        middle = float(keys[int(np.argmax(totals))])

    inside = [clip for clip, key in zip(voiced, keys) if abs(key - middle) <= SOLO_BAND]
    share = sum(clip["duration"] for clip in inside) / sum(clip["duration"] for clip in voiced)
    print(f"clustered {len(voiced)} of the cleanest clips by pitch: "
          f"{2 ** (middle / 12.0):.0f} Hz +-{SOLO_BAND:.0f} semitones holds "
          f"{len(inside)} of them, {share * 100:.0f}% of that audio")
    if share < 0.35:
        print("  that band is a minority of the clean speech, so the episode may "
              "have no\n  dominant voice. Listen to the reference before rendering "
              "a whole episode.")

    return inside


def list_troupe(directory):
    """Print the voice banks sitting under a directory.

    A solo dub is cast by pointing the render at one of these, so the useful
    view is a shelf of them: who each one is, how they sit, and where they
    were cut from. Anything with a bank.json counts.
    """
    banks = sorted(directory.glob("*/bank.json")) if directory.is_dir() else []
    if not banks:
        raise SystemExit(f"no voice banks under {directory} — mint one with "
                         f"--solo -o {directory}/<name>/")

    print(f"{'ACTOR':<18}{'pitch':>7}{'ref':>8}   cut from")
    print("-" * 78)
    for path in banks:
        for speaker, entry in json.loads(path.read_text()).items():
            print(f"{entry.get('actor', speaker):<18}{entry['pitch']:>6}Hz"
                  f"{entry['seconds']:>7.1f}s   {entry.get('cut_from', '-')}")
    print(f"\nCast one by pointing the render at its directory:\n"
          f"  scripts/dub_render.py ... {banks[0].parent} ...")
    return 0


def adopt_reference(clip_path, output_dir):
    """Take the solo actor's voice from a file instead of from the show.

    The pitch cluster gets the show's own dominant voice, which is usually
    what a solo dub wants. Where it is not — the lead is wrong for the
    register, or the episode has no dominant voice to find — any few seconds
    of clean speech will do, and this is where they go in.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audio, rate = sf.read(clip_path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)

    path = output_dir / f"{SOLO_ACTOR}.wav"
    sf.write(path, audio, rate)
    return {SOLO_ACTOR: {"path": str(path), "seconds": round(audio.size / rate, 2),
                         "clips": 1, "ratio": 0.0,
                         "pitch": round(median_pitch(audio, rate)),
                         "lines": [f"supplied: {Path(clip_path).name}"]}}


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


def label_bank(bank, actor, cut_from):
    """Record who a bank stands for and where the voice came from.

    Kept inside the entry rather than in the directory name, so a render can
    say who read it and a troupe can be listed without opening every wav.
    """
    for entry in bank.values():
        if actor:
            entry["actor"] = actor
        if cut_from:
            entry["cut_from"] = cut_from
    return bank


def write_bank(bank, understudies, output_dir):
    Path(output_dir, "bank.json").write_text(
        json.dumps(bank, indent=1, ensure_ascii=False))
    Path(output_dir, "understudies.json").write_text(
        json.dumps(understudies, indent=1, ensure_ascii=False))


def report_bank(bank, understudies, output_dir, solo):
    print(f"{'CHARACTER':<16}{'ref':>7}{'clips':>7}{'clean':>8}{'pitch':>8}   cloned from")
    print("-" * 86)
    for speaker, entry in sorted(bank.items(), key=lambda item: -item[1]["pitch"]):
        print(f"{speaker:<16}{entry['seconds']:>6.1f}s{entry['clips']:>7}"
              f"{entry['ratio']:>7.0f}x{entry['pitch']:>7}Hz   {entry['lines'][0][:32]}")

    thin = [speaker for speaker, entry in bank.items() if entry["seconds"] < 3.0]
    print(f"\n{len(bank)} voices banked in {output_dir}")
    if thin:
        print(f"thin, pool more episodes: {', '.join(sorted(thin))}")
    if solo:
        print(f"\nListen to {Path(output_dir, SOLO_ACTOR + '.wav')} before rendering an "
              f"episode.\nEvery line in the dub is read in that voice, so it is the one "
              f"thing worth\nbeing sure of. --reference replaces it; --solo-pitch picks "
              f"a different band.")

    if understudies:
        print(f"\nno clean audio, standing in with the nearest voice:")
        for speaker, entry in sorted(understudies.items(), key=lambda i: -i[1]["lines"]):
            print(f"  {speaker:<14}{entry['pitch']:>5}Hz -> {entry['voice']:<12}"
                  f"{entry['lines']:>3} lines")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", help="directory for the voice bank")
    parser.add_argument("--episode", nargs=2, action="append",
                        metavar=("UTTERANCES", "STEMS"),
                        help="an episode's utterance JSON and Demucs stem directory; "
                             "repeat to pool a supporting cast across a season")
    parser.add_argument("--solo", action="store_true",
                        help="mint one voice for the whole episode instead of a cast")
    parser.add_argument("--reference", metavar="WAV",
                        help="use this clip as the solo actor's voice rather than "
                             "cutting one out of the show")
    parser.add_argument("--solo-pitch", type=float, metavar="HZ",
                        help="cluster the solo reference around this pitch instead of "
                             "the densest band, when the automatic pick took the "
                             "wrong voice")
    parser.add_argument("--from-role", metavar="ROLE",
                        help="cut the solo voice from the lines a labelling gave to "
                             "one character, rather than from the episode's dominant "
                             "voice — this is how a troupe gets a second voice type")
    parser.add_argument("--actor", metavar="NAME",
                        help="the performer this bank stands for, recorded so a render "
                             "can say who read it")
    parser.add_argument("--troupe", metavar="DIR",
                        help="list the voice banks under this directory and stop")
    args = parser.parse_args()

    if args.troupe:
        return list_troupe(Path(args.troupe))
    if not args.output:
        parser.error("-o is required")

    # A supplied reference is already the voice. Nothing about the show is
    # needed to bank it, so none of the show is loaded — which is what makes
    # standing up a troupe member cheap.
    if args.reference:
        bank = label_bank(adopt_reference(args.reference, args.output),
                          args.actor, args.from_role or Path(args.reference).stem)
        write_bank(bank, {}, args.output)
        report_bank(bank, {}, args.output, solo=True)
        return 0

    if not args.episode:
        parser.error("at least one --episode is required")

    # Pooling matters because a minor character speaks a few clean seconds per
    # episode. Across a season that adds up to a clonable voice, while any one
    # episode gives too little.
    pooled, everyone, voice_parts, music_parts, rate = [], [], [], [], None
    played = 0                      # samples of audio already laid down
    for utterance_path, stem_dir in args.episode:
        utterances = json.loads(Path(utterance_path).read_text())
        episode_vocals, episode_music, rate = load_stems(stem_dir)

        for candidate in score_clips(utterances, episode_vocals, episode_music, rate,
                                     args.solo, args.from_role):
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

    if args.solo:
        if args.from_role and not pooled:
            raise SystemExit(f"no clean clips on any line labelled {args.from_role} "
                             f"— check the role's spelling against the utterances, "
                             f"and pool more episodes if it has few lines")
        pooled = mint_solo(pooled, vocals, rate, args.solo_pitch)
    bank = label_bank(build_bank(pooled, vocals, rate, args.output),
                      args.actor, args.from_role)

    # A solo read has one voice by construction, so there is nobody left over
    # to stand in for. Written empty all the same, so a bank switched from a
    # cast to a solo does not keep casting the cast's understudies.
    understudies = ({} if args.solo
                    else cast_understudies(everyone, bank, vocals, music, rate))
    write_bank(bank, understudies, args.output)
    report_bank(bank, understudies, args.output, args.solo)

    return 0


if __name__ == "__main__":
    sys.exit(main())
