#!/usr/bin/env python3
"""Repackage one finished dub as an MP4 that plays anywhere, to send someone.

A dub in the library is a Matroska file carrying the dub, the original audio
and both subtitle tracks. That is the right shape for Emby and the wrong shape
for handing to a person: chat clients play MP4 inline and send Matroska as a
file attachment, and a second audio track is a coin toss over which language
the recipient hears.

So this keeps the dub and drops the rest. What it does *not* do, unless it has
to, is re-encode: these renders come out H.264 High in yuv420p with AAC-LC
beside them, which is already exactly what a phone wants, and a re-encode
would only spend an hour of CPU making it slightly worse. The video is copied
and the container changes. Ask for a height and it transcodes instead, which
is worth it only when the file has to be smaller rather than better.

Usage:
    python3 scripts/dub_share.py "Shirokuma Cafe" 1
    python3 scripts/dub_share.py "Shirokuma Cafe" 1 --height 720
    python3 scripts/dub_share.py "dub/finished/tv/Show/Season 01/S01E01 ... .mkv"
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dub_season import ROOT, episodes, read_plan, resolve  # noqa: E402

# Where a file made to be sent to somebody goes. Deliberately not the library:
# it is a lossy, single-audio copy of something the library already holds
# properly, and Emby should never see it.
OUTBOX = ROOT / "dub" / "share"

# What plays without argument on a phone, a browser and a desktop chat client.
# Anything outside this gets transcoded rather than copied.
PLAYABLE = {"h264"}, {"yuv420p"}, 51


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["streams"]


def dub_track(streams):
    """Which audio is the dub.

    By its title first, because that is what dub_render.py wrote and it is
    unambiguous. Falling back to the first English track, then to the first
    track at all, so this still works on something muxed by hand.
    """
    audio = [s for s in streams if s["codec_type"] == "audio"]
    if not audio:
        raise SystemExit("no audio in that file")
    named = [s for s in audio if "dub" in s.get("tags", {}).get("title", "").lower()]
    english = [s for s in audio if s.get("tags", {}).get("language") == "eng"]
    return (named or english or audio)[0]


def find_episode(show, number):
    paths = resolve(show)
    if len(paths) > 1:
        raise SystemExit("name which show: "
                         + ", ".join(read_plan(path)["show"] for path in paths))
    plan = read_plan(paths[0])
    for entry in episodes(plan):
        if entry["number"] == number:
            if not entry["output"].exists():
                raise SystemExit(f"{plan['show']} episode {number} is not rendered yet")
            return entry["output"]
    raise SystemExit(f"{plan['show']} has no episode {number}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show", help="show title, any of its aliases, or a path to an mkv")
    parser.add_argument("episode", nargs="?", type=int,
                        help="episode number, when the first argument is a title")
    parser.add_argument("--height", type=int, metavar="N",
                        help="transcode down to this height; omit to copy the "
                             "video untouched, which is faster and lossless")
    parser.add_argument("--crf", type=int, default=21,
                        help="quality when transcoding, lower is better (default 21)")
    parser.add_argument("-o", "--output", help="where to write it")
    args = parser.parse_args()

    source = Path(args.show)
    if not source.is_file():
        if args.episode is None:
            raise SystemExit("give an episode number, or a path to an mkv")
        source = find_episode(args.show, args.episode)

    streams = probe(source)
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = dub_track(streams)

    codecs, formats, level = PLAYABLE
    copyable = (args.height is None
                and video["codec_name"] in codecs
                and video.get("pix_fmt") in formats
                and int(video.get("level") or 0) <= level)

    if copyable:
        picture = ["-c:v", "copy"]
        how = f"copied the video untouched ({video['codec_name']} " \
              f"{video['width']}x{video['height']})"
    else:
        scale = (["-vf", f"scale=-2:{args.height}"] if args.height else [])
        picture = [*scale, "-c:v", "libx264", "-preset", "medium",
                   "-crf", str(args.crf), "-pix_fmt", "yuv420p",
                   "-profile:v", "high", "-level", "4.0"]
        how = (f"transcoded to {args.height}p" if args.height
               else f"transcoded ({video['codec_name']} is not playable everywhere)")

    # AAC-LC stereo is what these renders already carry, so this is normally
    # another copy. Re-encoded only where it is something else.
    sound = (["-c:a", "copy"] if audio["codec_name"] == "aac"
             else ["-c:a", "aac", "-b:a", "192k"])

    output = Path(args.output) if args.output else OUTBOX / (source.stem + ".mp4")
    output.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(["ffmpeg", "-v", "error", "-stats", "-i", str(source),
                    "-map", f"0:{video['index']}", "-map", f"0:{audio['index']}",
                    *picture, *sound,
                    # No subtitles: chat clients do not render them and some
                    # refuse to play a file inline once it carries any.
                    "-sn",
                    # The index goes at the front, so it plays while it arrives
                    # rather than only once the whole file has.
                    "-movflags", "+faststart",
                    "-metadata:s:a:0", "title=English (AI dub)",
                    str(output), "-y"], check=True)

    size = output.stat().st_size / 1e6
    print(f"\n{how}")
    print(f"kept the {audio.get('tags', {}).get('title', 'first')} audio, dropped "
          f"the rest\nwrote {output} ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
