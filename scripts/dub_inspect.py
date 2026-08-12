#!/usr/bin/env python3
"""Compare a dubbed track against the original it was built from.

For when a mix sounds wrong but not obviously wrong. The two tracks share a
music and effects bed, so anywhere the dub departs from the original outside
the dialogue itself is the pipeline doing something it was not asked to.

The comparison is a log-mel spectral difference, in decibels per band per
frame. That is an audio measure, deliberately: an image-difference metric such
as FLIP models how a person sees a rendered picture, and a spectrogram is a
plot rather than something anyone listens to, so a visual metric would be
scoring the colormap. For a standards-grade perceptual score use ViSQOL or
PEAQ; for finding what broke, a banded difference says where and when, which a
single score never does.

Writes a three-panel image, and prints the numbers the image is hard to read
precisely: which bands drifted, and whether the bed changes level as lines
start and stop, which is audible as the score pumping under the dialogue.

Usage:
    python3 scripts/dub_inspect.py dub/preview/cafe_crew.mkv -o inspect.png
    python3 scripts/dub_inspect.py DUBBED.mkv --timing DUBBED.mkv.timing.json -o out.png
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

MELS = 96
HOP = 512
TOP_DB = 80


def extract_track(video, index, rate=44100):
    out = Path(tempfile.mkdtemp()) / f"track{index}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", f"0:a:{index}",
                    "-ac", "1", "-ar", str(rate), str(out), "-y"], check=True)
    audio, got = sf.read(out, dtype="float32")
    return audio, got


def mel_spectrogram(audio, rate):
    import librosa

    power = librosa.feature.melspectrogram(y=audio, sr=rate, n_mels=MELS,
                                           hop_length=HOP, fmax=rate / 2)
    return librosa.power_to_db(power, ref=np.max, top_db=TOP_DB)


def band_report(reference, dubbed, rate):
    """Mean difference per frequency band, in decibels."""
    import librosa

    edges = librosa.mel_frequencies(n_mels=MELS, fmax=rate / 2)
    groups = [("sub 0-120", 0, 120), ("low 120-400", 120, 400),
              ("voice 400-1k", 400, 1000), ("voice 1k-3.4k", 1000, 3400),
              ("presence 3.4k-8k", 3400, 8000), ("air 8k+", 8000, rate / 2)]

    rows = []
    for name, low, high in groups:
        which = (edges >= low) & (edges < high)
        if not which.any():
            continue
        rows.append((name, float(np.mean(dubbed[which] - reference[which]))))
    return rows


def bed_stability(reference, dubbed, timing, rate, hop_seconds):
    """Does the music sit at a different level while a line is playing?

    The bed is common to both tracks, so outside the dialogue the two should
    agree. If they disagree by more than the dialogue explains, the bed is
    being gained up or down as lines start and stop, and the score audibly
    breathes under the dub.
    """
    if not timing:
        return None

    frames = reference.shape[1]
    speaking = np.zeros(frames, dtype=bool)
    for row in timing:
        start = row.get("start_seconds")
        if start is None:
            continue
        head = int(start / hop_seconds)
        tail = min(frames, head + max(1, int(row.get("held", 2.0) / hop_seconds)))
        speaking[max(0, head):tail] = True

    if not speaking.any() or speaking.all():
        return None

    # Below the voice band the dialogue contributes little, so a difference
    # there is the bed itself moving rather than speech being added.
    low = slice(0, MELS // 6)
    during = float(np.mean(dubbed[low][:, speaking] - reference[low][:, speaking]))
    between = float(np.mean(dubbed[low][:, ~speaking] - reference[low][:, ~speaking]))
    return during, between


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", help="a rendered dub, holding both audio tracks")
    parser.add_argument("-o", "--output", required=True, help="PNG to write")
    parser.add_argument("--dub-track", type=int, default=0)
    parser.add_argument("--original-track", type=int, default=1)
    parser.add_argument("--timing", help="the .timing.json beside the render")
    args = parser.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dub_audio, rate = extract_track(args.video, args.dub_track)
    original_audio, _ = extract_track(args.video, args.original_track)
    length = min(dub_audio.size, original_audio.size)
    dub_audio, original_audio = dub_audio[:length], original_audio[:length]

    dubbed = mel_spectrogram(dub_audio, rate)
    reference = mel_spectrogram(original_audio, rate)
    frames = min(dubbed.shape[1], reference.shape[1])
    dubbed, reference = dubbed[:, :frames], reference[:, :frames]
    difference = dubbed - reference

    seconds = length / rate
    extent = [0, seconds, 0, MELS]
    figure, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    for panel, (data, title, kwargs) in enumerate([
            (reference, "original", dict(cmap="magma", vmin=-TOP_DB, vmax=0)),
            (dubbed, "dubbed", dict(cmap="magma", vmin=-TOP_DB, vmax=0)),
            (difference, "difference (dub minus original, dB)",
             dict(cmap="coolwarm", vmin=-24, vmax=24))]):
        image = axes[panel].imshow(data, origin="lower", aspect="auto",
                                   extent=extent, **kwargs)
        axes[panel].set_title(title)
        axes[panel].set_ylabel("mel band")
        figure.colorbar(image, ax=axes[panel], pad=0.01)
    axes[-1].set_xlabel("seconds")
    figure.tight_layout()
    figure.savefig(args.output, dpi=110)

    print(f"{seconds:.1f}s compared, {frames} frames, {MELS} mel bands\n")
    print(f"{'BAND':<20}{'dub - original':>16}")
    print("-" * 38)
    for name, delta in band_report(reference, dubbed, rate):
        flag = "   <- large" if abs(delta) > 3.0 else ""
        print(f"{name:<20}{delta:>13.1f} dB{flag}")

    timing = json.loads(Path(args.timing).read_text()) if args.timing else None
    stability = bed_stability(reference, dubbed, timing, rate, HOP / rate)
    if stability:
        during, between = stability
        print(f"\nbed below the voice band: {during:+.1f} dB while speaking, "
              f"{between:+.1f} dB between lines")
        if abs(during - between) > 1.0:
            print(f"  the bed moves {abs(during - between):.1f} dB as lines start and stop, "
                  f"which is the score breathing under the dialogue")

    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
