#!/usr/bin/env python3
"""Decide whether this machine can dub, before anyone is told that it can.

Dubbing needs a CUDA GPU, several GB of model weights and roughly a quarter
hour of GPU time per episode. On a box without those it is not slow, it is
impractical, and offering it there wastes the user's time and disk on a
setup that will not finish. So the capability is gated on this check rather
than advertised unconditionally.

Exit status is the answer, so it can gate a shell step:
    0  this machine can dub
    1  it cannot

Usage:
    python3 scripts/dub_check.py           # one line, for an agent to read
    python3 scripts/dub_check.py --full    # every measurement and why
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# IndexTTS-2 peaks near 6 GB and Demucs wants headroom beside it. Below this a
# run either fails outright or thrashes into shared memory.
MIN_VRAM_MB = 8000

# Weights are about 12 GB once the vocoder and w2v-bert are cached, and each
# prepared episode costs roughly 1.1 GB in video and stems.
MIN_FREE_GB = 40


def probe_gpu():
    """Return (name, total VRAM in MB) for the largest CUDA device, or None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    cards = []
    for row in result.stdout.strip().splitlines():
        name, _, memory = row.rpartition(",")
        try:
            cards.append((name.strip(), int(memory.strip())))
        except ValueError:
            continue
    return max(cards, key=lambda card: card[1]) if cards else None


def has_rubberband():
    """Time-fitting needs rubberband; atempo leaves artefacts on speech."""
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "rubberband" in result.stdout


def free_gigabytes(where):
    try:
        return shutil.disk_usage(where).free / 1e9
    except OSError:
        return 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="show every measurement")
    parser.add_argument("--path", default=".", help="where the workspace would live")
    args = parser.parse_args()

    gpu = probe_gpu()
    free = free_gigabytes(Path(args.path).resolve())
    rubberband = has_rubberband()

    blockers = []
    if gpu is None:
        blockers.append("no CUDA GPU (nvidia-smi found nothing)")
    elif gpu[1] < MIN_VRAM_MB:
        blockers.append(f"{gpu[0]} has {gpu[1]} MB VRAM, needs {MIN_VRAM_MB}")
    if free < MIN_FREE_GB:
        blockers.append(f"{free:.0f} GB free, needs {MIN_FREE_GB}")
    if not rubberband:
        blockers.append("ffmpeg has no rubberband filter")

    if args.full:
        print(f"GPU         {gpu[0] + f' ({gpu[1]} MB)' if gpu else 'none'}")
        print(f"free disk   {free:.0f} GB at {Path(args.path).resolve()}")
        print(f"rubberband  {'yes' if rubberband else 'no'}")
        print()

    if blockers:
        print("cannot dub: " + "; ".join(blockers))
        return 1

    print(f"can dub: {gpu[0]}, {gpu[1]} MB VRAM, {free:.0f} GB free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
