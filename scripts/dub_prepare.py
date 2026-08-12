#!/usr/bin/env python3
"""Prepare episodes for dubbing: fetch, split the stems, parse the script.

Every later stage needs the same three things per episode — a local copy of
the video, the vocals and music-and-effects stems, and the utterance list. This
tool produces all three and skips whatever is already done, so it can be re-run
over a whole season without repeating work.

The library holds `.strm` files pointing at Torbox rather than real video, so
an episode is fetched to local disk first. Dubbing reads the file many times
and re-encodes from it, which is not something to do over a streaming link.

Usage:
    python3 scripts/dub_prepare.py "library/tv/Polar Bear Cafe/Season 01" --limit 4
    python3 scripts/dub_prepare.py "library/tv/Show/Season 01" --work dub
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

DEMUCS_MODEL = "htdemucs"

# Torbox's edge routinely drops a TLS handshake or answers 403/520 under load
# (see torbox_sync.py's api_get for the same fight). A plain curl -sL treats
# that as success and writes the error page to disk, which then fails
# confusingly several steps later inside ffmpeg instead of here.
FETCH_ATTEMPTS = 4

# A real episode is hundreds of MB. Anything under this is an error page or a
# truncated stream, not a video, however cleanly curl exited.
MIN_VIDEO_SIZE = 10_000_000


def episode_key(path):
    """Sort by episode number so --limit takes the first episodes, not a jumble."""
    match = re.search(r"[Ss](\d+)[Ee](\d+)", path.name)
    return (int(match.group(1)), int(match.group(2))) if match else (99, 99)


def slug(path):
    match = re.search(r"[Ss](\d+)[Ee](\d+)", path.name)
    return f"s{int(match.group(1)):02d}e{int(match.group(2)):02d}" if match else path.stem[:24]


def fetch(source, destination):
    """Copy a local file, or download whatever the .strm points at.

    A resume check that only asks "does a file exist" trusts a previous
    failed download exactly as much as a good one. Torbox's edge answers
    403/520 for real often enough that this needs its own retry, the same
    fight torbox_sync.py's api_get already has.
    """
    if destination.exists() and destination.stat().st_size >= MIN_VIDEO_SIZE:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix != ".strm":
        destination.symlink_to(source.resolve())
        return destination

    url = source.read_text().strip()
    for attempt in range(FETCH_ATTEMPTS):
        subprocess.run(["curl", "-sL", "--fail", "--retry", "3",
                        "-o", str(destination), url])
        if destination.exists() and destination.stat().st_size >= MIN_VIDEO_SIZE:
            return destination
        destination.unlink(missing_ok=True)
        if attempt < FETCH_ATTEMPTS - 1:
            print(f"  [retry] fetch of {source.name} came back empty or too "
                  f"small, retrying", file=sys.stderr)
            time.sleep(2 ** attempt * 5)

    raise SystemExit(f"could not fetch {source.name}: every attempt came back "
                     f"empty or too small (Torbox may be having a bad moment; "
                     f"re-run to pick up where this left off)")


def split_stems(video, stem_root, slug_name, venv_python):
    """Separate the voices from the music and effects bed.

    Demucs names its output directory after the file it was given, so the
    working wav and the stem directory have to be derived from one name. Deriving
    them separately produced `s01e02.audio.audio`, which the resume check then
    failed to find, quietly re-splitting an episode that was already done.
    """
    stem_dir = stem_root / DEMUCS_MODEL / f"{slug_name}.audio"
    if (stem_dir / "no_vocals.wav").exists():
        return stem_dir

    audio = stem_root.parent / "work" / f"{slug_name}.audio.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    if not audio.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0",
                        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le",
                        str(audio), "-y"], check=True)

    # The GPU queue keeps parallel agent sessions from oversubscribing the card.
    # {track} keeps each episode in its own directory. Without it every
    # episode writes over the same pair of stem files.
    subprocess.run(["processqueue", "gpu", str(venv_python), "-m", "demucs",
                    "--two-stems=vocals", "-n", DEMUCS_MODEL, "-o", str(stem_root),
                    "--filename", "{track}/{stem}.wav", str(audio)], check=True)
    audio.unlink(missing_ok=True)
    return stem_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("season", help="a season directory, or a single episode")
    parser.add_argument("--work", default="dub", help="working directory (default: dub)")
    parser.add_argument("--limit", type=int, help="only prepare this many episodes")
    args = parser.parse_args()

    season = Path(args.season)
    sources = sorted([season] if season.is_file()
                     else [*season.glob("*.strm"), *season.glob("*.mkv")], key=episode_key)
    if args.limit:
        sources = sources[:args.limit]
    if not sources:
        raise SystemExit(f"no episodes found in {season}")

    work = Path(args.work)
    venv_python = work / ".venv" / "bin" / "python"
    script_tool = Path(__file__).parent / "dub_script.py"
    prepared = []

    for source in sources:
        name = slug(source)
        print(f"\n=== {name}  {source.name}")

        video = fetch(source, work / "source" / f"{name}.mkv")
        print(f"  video   {video.stat().st_size / 1e6:.0f} MB")

        stem_dir = split_stems(video, work / "stems", name, venv_python)
        print(f"  stems   {stem_dir}")

        utterances = work / "work" / f"{name}.utterances.json"
        if not utterances.exists():
            subprocess.run([sys.executable, str(script_tool), str(video),
                            "-o", str(utterances)], check=True)
        print(f"  script  {utterances}")
        prepared.append((utterances, stem_dir))

    print(f"\nprepared {len(prepared)} episodes. Mint the voice bank with:\n")
    pairs = " \\\n    ".join(f"--episode {utterance} {stem}" for utterance, stem in prepared)
    print(f"  {venv_python} scripts/dub_voices.py -o {work}/voices/ \\\n    {pairs}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
