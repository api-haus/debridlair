#!/usr/bin/env python3
"""Line up a subtitle track that was timed against a different release.

The release with the labelled fansub is often not the release worth dubbing,
and the two are rarely timed alike: a different encode starts its episode a
second and a half later, or carries the network logo the other cut out. Fed to
the dub as they are, every line is spoken over the wrong shot.

Two releases of one edition differ by a single constant, so this measures that
constant and applies it. It measures it twice over, by two methods that fail
differently, and it says which one answered:

  - By text, when both sides are subtitles. Several groups routinely ship the
    same translation, so the same sentence can be found on both sides and the
    gap between them read off directly. Exact, and it reports the scatter, so
    a good answer is visibly good.
  - By activity, otherwise. Where the text differs (or the target has no
    subtitles at all) the shape of the episode still matches: speech and
    silence fall in the same places. Cross-correlating the two says where.

Then it checks the constant is actually constant. A subtitle script pulled
from a PAL transfer runs 4% fast against a film-rate encode and no single
offset fits it; that is a different problem and this refuses to paper over it.

Usage:
    python3 scripts/dub_align.py --subs labelled.ass --against dub/source/s01e01.mkv
    python3 scripts/dub_align.py --subs labelled.ass --against dub/stems/htdemucs/s01e01.audio \\
        -o dub/subs/s01e01.ass
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

SUBTITLE_SUFFIXES = (".ass", ".ssa", ".srt", ".vtt")
VIDEO_SUFFIXES = (".mkv", ".mp4", ".webm", ".m4v", ".avi")

# The timelines are compared at this resolution, in seconds. Finer than a
# subtitle is cued by hand and finer than a viewer can tell.
FRAME = 0.05

# How far apart the two releases may be. Wider than any sane difference
# between two encodes of one edition, and narrow enough that the search stays
# cheap and cannot lock onto a repeat somewhere else in the episode.
MAX_OFFSET = 180.0

# A text anchor has to be long enough that finding it twice means something.
# "Yes." appears in every episode ever made.
MIN_ANCHOR = 14

# Anchors scatter a little because two groups cue the same line a few frames
# apart. Past this the two scripts are not the same script and the median is
# averaging unrelated numbers.
MAX_SCATTER = 0.5

# A correlation peak this weak is not a match, it is the best of a bad field.
MIN_PEAK = 0.25

# The peak has to stand clear of the rest of the search. A track that scores
# nearly as well two seconds either side has not been located to a second.
MIN_MARGIN = 0.05

# How much the offset may differ between the start and the end of the episode
# before a single constant is the wrong model.
MAX_DRIFT = 0.6

# Frame-rate pairs that produce a drifting offset, and what a script timed
# against one reads as against the other.
RATE_PAIRS = ((25.0 / 23.976, "PAL 25 fps against film 23.976"),
              (24.0 / 23.976, "24 fps against 23.976"),
              (30.0 / 29.97, "30 fps against 29.97"))


def parse_timestamp(stamp):
    hours, minutes, seconds = stamp.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def read_subtitle_events(path):
    """Every cue in an ASS or SRT file, as (start, end, text)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    events = []

    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            fields = line[len("Dialogue:"):].split(",", 9)
            if len(fields) >= 10:
                events.append((parse_timestamp(fields[1]), parse_timestamp(fields[2]),
                               fields[9]))

    if events:
        return sorted(events)

    # SRT: a timing line, then the cue text until a blank line.
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        timing = re.search(r"(\d+:\d\d:\d\d[,.]\d+)\s*-->\s*(\d+:\d\d:\d\d[,.]\d+)", block)
        if timing:
            body = block[timing.end():].strip()
            events.append((parse_timestamp(timing.group(1)),
                           parse_timestamp(timing.group(2)), body))

    return sorted(events)


def subtitle_track(video_path):
    """Extract a text subtitle track from a video, if it carries one.

    Any track will do, and that is deliberate — this is not the track being
    dubbed, it is a ruler. A Japanese track, or a signs-only one, is cued to
    the same speech and lines the episode up just as well, so nothing here
    asks about language the way dub_script.py has to.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index,codec_name", "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True)
    streams = [stream for stream in json.loads(probe.stdout).get("streams", [])
               if stream.get("codec_name") in ("ass", "ssa", "subrip", "mov_text")]
    if not streams:
        return None

    destination = Path(tempfile.mkdtemp()) / "reference.ass"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video_path),
                    "-map", f"0:{streams[0]['index']}", str(destination), "-y"],
                   check=True)
    return destination


def clean_text(raw):
    """The words of a cue, with the typesetting taken off."""
    stripped = re.sub(r"\{[^}]*\}", "", raw)
    stripped = re.sub(r"</?[a-z][^>]*>", " ", stripped)
    stripped = stripped.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    return re.sub(r"[^a-z0-9 ]", "", stripped.lower()).strip()


def audio_activity(path, duration):
    """Where there is sound, frame by frame, from any audio ffmpeg can read.

    Only a fallback. It cannot tell speech from score, so on a busy soundtrack
    it describes the mix rather than the dialogue and the correlation loosens
    accordingly. Point it at a Demucs vocals stem where one exists and it is
    describing the same thing the subtitles are.
    """
    source = Path(path)
    if source.is_dir():
        source = source / "vocals.wav"
        if not source.exists():
            raise SystemExit(f"{path} is a directory with no vocals.wav in it")

    # Piped as raw PCM rather than written as a wav and read back with
    # soundfile, which keeps this tool runnable on the system Python. Only the
    # audio fallback would need the dub virtualenv, and it is the path least
    # worth walking into a missing dependency on.
    rate = 16000
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a:0",
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    samples = np.frombuffer(decoded, dtype="<i2").astype("float32") / 32768.0
    width = int(FRAME * rate)
    usable = (samples.size // width) * width
    energy = np.sqrt(np.mean(np.square(samples[:usable].reshape(-1, width)), axis=1))

    # An absolute threshold would follow how loudly the episode was mastered.
    # A high percentile of the frame energies is the level this episode's own
    # loud moments sit at, whatever that is.
    loud = np.percentile(energy, 90)
    timeline = (energy > 0.25 * loud).astype("float32")
    return timeline[:duration] if duration else timeline


def subtitle_activity(events, frames):
    """Where text is on screen, frame by frame."""
    timeline = np.zeros(frames, dtype="float32")
    for start, end, _ in events:
        head, tail = int(start / FRAME), min(int(end / FRAME), frames)
        if tail > head:
            timeline[head:tail] = 1.0
    return timeline


def correlate(moving, fixed):
    """The shift that lines two timelines up, and how clearly it stands out.

    Scored as a normalised correlation over the overlapping part alone, so a
    shift is not rewarded for pushing the mismatched tail off the end.
    """
    reach = int(MAX_OFFSET / FRAME)
    # A shift that leaves the two barely overlapping can score well on the
    # sliver that remains, so a minimum overlap is demanded. Half the shorter
    # timeline rather than a fixed span, or aligning anything shorter than the
    # search window scores every shift zero.
    least = min(reach, min(moving.size, fixed.size) // 2)
    scores = []
    for shift in range(-reach, reach + 1):
        if shift >= 0:
            left, right = moving[:moving.size - shift or None], fixed[shift:]
        else:
            left, right = moving[-shift:], fixed[:fixed.size + shift or None]
        span = min(left.size, right.size)
        if span < least:
            scores.append(0.0)
            continue
        a, b = left[:span] - left[:span].mean(), right[:span] - right[:span].mean()
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        scores.append(float(np.dot(a, b) / norm) if norm else 0.0)

    scores = np.asarray(scores)
    best = int(np.argmax(scores))
    # How far the peak stands above the rest of the field. A second peak a
    # long way off is a repeated pattern; one right beside it is the same
    # peak, so its own neighbourhood is excluded.
    guard = int(2.0 / FRAME)
    field = np.concatenate([scores[:max(0, best - guard)], scores[best + guard:]])
    margin = float(scores[best] - field.max()) if field.size else float(scores[best])
    return (best - reach) * FRAME, float(scores[best]), margin


def anchor_offsets(moving_events, fixed_events):
    """Per-line offsets read off lines whose text appears once on each side."""
    def index(events):
        counts, positions = {}, {}
        for start, _, raw in events:
            key = clean_text(raw)
            if len(key) < MIN_ANCHOR:
                continue
            counts[key] = counts.get(key, 0) + 1
            positions.setdefault(key, start)
        return {key: positions[key] for key, count in counts.items() if count == 1}

    moving, fixed = index(moving_events), index(fixed_events)
    shared = set(moving) & set(fixed)
    return sorted((moving[key], fixed[key] - moving[key]) for key in shared)


def fit_anchors(pairs):
    """Fit the per-line offsets as a constant, and as a straight line.

    Both, because which one fits is the finding. Scattered around a constant,
    the two scripts share some lines but are not the same script. Tight around
    a sloping line, they are the same script timed against a different
    transfer, and no single offset exists to be found — which is worth saying
    outright rather than reporting as a failure to match.
    """
    times = np.asarray([at for at, _ in pairs])
    offsets = np.asarray([offset for _, offset in pairs])

    middle = float(np.median(offsets))
    scatter = float(np.median(np.abs(offsets - middle)))

    slope, intercept = (np.polyfit(times, offsets, 1) if len(pairs) >= 6
                        else (0.0, middle))
    residual = float(np.median(np.abs(offsets - (slope * times + intercept))))
    return {"offset": middle, "scatter": scatter, "slope": float(slope),
            "residual": residual, "span": float(times.max() - times.min())}


def shift_file(source, destination, offset, window=None):
    """Rewrite a subtitle file with every timestamp moved.

    `window` is an optional (start, end) in the *shifted* times, and cues
    falling wholly outside it are dropped rather than clamped. Clamping is
    right when the whole file is being realigned — a negative timestamp is not
    a time — but wrong when a span is being cut out of an episode, where every
    earlier cue would otherwise pile up as a zero-length event at the start.
    286 of them did.
    """
    def moved(stamp):
        return parse_timestamp(stamp) + offset

    def written(seconds, srt):
        hours, rest = divmod(max(0.0, seconds), 3600)
        minutes, seconds = divmod(rest, 60)
        if srt:
            return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}".replace(".", ",")
        return f"{int(hours)}:{int(minutes):02d}:{seconds:05.2f}"

    def inside(head, tail):
        return window is None or (tail >= window[0] and head <= window[1])

    text = Path(source).read_text(encoding="utf-8-sig", errors="replace")
    pattern = re.compile(r"\d+:\d\d:\d\d[,.]\d+")

    if "Dialogue:" in text:
        lines = []
        for line in text.splitlines():
            # An ASS event's timestamps are its second and third fields, and
            # only those. Rewriting every timestamp-shaped run on the line
            # would also rewrite anything in the cue text that looks like one.
            if line.startswith(("Dialogue:", "Comment:")):
                head, _, rest = line.partition(":")
                fields = rest.split(",", 9)
                if len(fields) >= 10:
                    start, end = moved(fields[1].strip()), moved(fields[2].strip())
                    if not inside(start, end):
                        continue
                    fields[1], fields[2] = written(start, False), written(end, False)
                    line = f"{head}:" + ",".join(fields)
            lines.append(line)
        body = "\n".join(lines) + "\n"
    else:
        # SRT carries its cue text on the lines after the timing, so a dropped
        # cue has to take its whole block with it, and what survives has to be
        # renumbered.
        blocks, kept = re.split(r"\n\s*\n", text.strip()), []
        for block in blocks:
            timing = re.search(r"(\d+:\d\d:\d\d[,.]\d+)\s*-->\s*(\d+:\d\d:\d\d[,.]\d+)",
                               block)
            if timing is None:
                continue
            start, end = moved(timing.group(1)), moved(timing.group(2))
            if not inside(start, end):
                continue
            rest = block[timing.end():].lstrip("\n")
            kept.append(f"{len(kept) + 1}\n{written(start, True)} --> "
                        f"{written(end, True)}\n{rest}")
        body = "\n\n".join(kept) + "\n"

    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(body, encoding="utf-8")


def reference_events(path):
    """The reference's own cues, when it has any."""
    source = Path(path)
    if source.suffix.lower() in SUBTITLE_SUFFIXES:
        return read_subtitle_events(source)
    if source.suffix.lower() in VIDEO_SUFFIXES:
        track = subtitle_track(source)
        return read_subtitle_events(track) if track else None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subs", required=True,
                        help="the subtitle file to line up, timed against another release")
    parser.add_argument("--against", required=True,
                        help="the release being dubbed: a video, its subtitle track, "
                             "an audio file, or a Demucs stem directory")
    parser.add_argument("-o", "--output", help="write the shifted subtitle file here")
    parser.add_argument("--offset", type=float, metavar="SECONDS",
                        help="skip the measurement and apply this offset")
    parser.add_argument("--force", action="store_true",
                        help="write the shifted file even when the measurement is poor")
    args = parser.parse_args()

    moving_events = read_subtitle_events(args.subs)
    if not moving_events:
        raise SystemExit(f"no subtitle cues in {args.subs}")

    if args.offset is not None:
        offset, confident = args.offset, True
        print(f"applying the offset given: {offset:+.3f}s")
    else:
        offset, confident = measure(moving_events, args.against)

    if args.output:
        if not confident and not args.force:
            raise SystemExit(
                "\nrefusing to write a shifted file on a measurement this poor. "
                "Look at\nthe numbers above: a wrong offset dubs the whole episode "
                "over the wrong\nshots and nothing downstream will notice. Pass "
                "--offset to set it by hand,\nor --force to write it anyway.")
        shift_file(args.subs, args.output, offset)
        print(f"\nwrote {args.output}")

    return 0 if confident else 1


def measure(moving_events, against):
    """Fit the offset, by text where possible and by activity otherwise."""
    fixed_events = reference_events(against)
    last = max(end for _, end, _ in moving_events)

    offset, confident = None, False
    if fixed_events:
        pairs = anchor_offsets(moving_events, fixed_events)
        if len(pairs) >= 8:
            fit = fit_anchors(pairs)
            end_to_end = fit["slope"] * fit["span"]
            print(f"{len(pairs)} lines occur once in both scripts and place the "
                  f"track {fit['offset']:+.3f}s\n(scatter {fit['scatter']:.3f}s "
                  f"across those lines)")

            if fit["scatter"] <= MAX_SCATTER and abs(end_to_end) <= MAX_DRIFT:
                offset, confident = fit["offset"], True
                print(f"  holds across the episode: {end_to_end:+.3f}s of drift "
                      f"end to end")
            elif fit["residual"] <= MAX_SCATTER and abs(end_to_end) > MAX_DRIFT:
                report_rate(fit)
                return fit["offset"], False
            else:
                print("  the anchors disagree too much to be one offset — these are "
                      "two different\n  scripts that share some lines, falling back "
                      "to the activity match")
        else:
            print(f"only {len(pairs)} lines occur once in both scripts, too few to "
                  f"read an offset off;\nfalling back to the activity match")

    if offset is None:
        frames = int(max(last, 60.0) / FRAME) + int(MAX_OFFSET / FRAME)
        moving = subtitle_activity(moving_events, frames)
        fixed = (subtitle_activity(fixed_events, frames) if fixed_events
                 else audio_activity(against, frames))
        found, peak, margin = correlate(moving, fixed)
        source = "its subtitle track" if fixed_events else "its audio"
        print(f"\nagainst {source}: the timelines match best {found:+.3f}s along "
              f"(peak {peak:.2f},\nstanding {margin:.2f} above the rest of the search)")
        offset = found
        confident = peak >= MIN_PEAK and margin >= MIN_MARGIN
        if not confident:
            print(f"  that is not a match: a real one peaks above {MIN_PEAK:.2f} and "
                  f"stands {MIN_MARGIN:.2f}\n  clear. These may not be the same "
                  f"episode, or the same edition of it.")

    if confident:
        print(f"\noffset {offset:+.3f}s — pass it to dub_script.py as an already "
              f"shifted file:")
        print(f"  python3 scripts/dub_align.py --subs ... --against ... -o shifted.ass")
    return offset, confident


def report_rate(fit):
    """Say so when the two scripts run at different rates.

    The anchors sit on a line rather than scattering, which means these are
    the same script and there is simply no constant to find. Fitting one
    anyway lines up the middle of the episode and walks off both ends.
    """
    ratio = 1.0 / (1.0 + fit["slope"])
    named = next((name for value, name in RATE_PAIRS
                  if abs(ratio - value) < 0.005 or abs(1 / ratio - value) < 0.005), None)

    print(f"\n  these are the same script at a different rate, not a different "
          f"script: the\n  offsets sit on a line to within {fit['residual']:.3f}s, "
          f"sliding {fit['slope'] * fit['span']:+.1f}s across\n  "
          f"{fit['span'] / 60:.0f} minutes — a rate ratio of {ratio:.4f}.")
    if named:
        print(f"  That is {named}. The script was timed against a different "
              f"transfer.")
    print("  No single offset fits this. Retime it with a rate conversion, or "
          "find a\n  script cut for this release.")


if __name__ == "__main__":
    sys.exit(main())
