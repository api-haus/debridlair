#!/usr/bin/env python3
"""Visualize and resolve a case where two characters' lines collide in time.

`dub_script.py --overdubs` finds the rare passages where separate, individually
written lines overlap in time — not a unison line, which already says who
speaks it together, but two characters simply talking over each other. The
mono, one-speaker-at-a-time render has nowhere to put the second voice but on
top of the first, and every such case is different: how far apart the original
mix panned the speakers, and how it balanced them, is a scene-by-scene
decision no fixed rule should make.

This tool builds the evidence for that decision and records it. It reads the
separated vocals stem — still stereo, carrying whatever the original mix did —
and for the case's span:

  - draws a stereo-panning spectrogram: color says which side of the stereo
    field each moment of sound sits on, opacity says how loud it is, so the
    picture shows where in the mix the colliding voices actually sit;
  - finds each speaker's nearest solo lines (this same case, no overlap) and
    measures their own pan and loudness there, as a starting estimate for
    where that character's dubbed voice belongs;
  - writes both into a JSON resolution file, one entry per case, with a
    `proposed` block from the measurement above and a `resolved` block left
    for a human or an LLM reviewing the image to fill in — that block, not the
    proposal, is what `dub_render.py` actually renders with.

Usage:
    python3 scripts/dub_overdub.py dub/work/s01e01.utterances.json \\
        dub/stems/htdemucs/s01e01.audio --case 1
    python3 scripts/dub_overdub.py dub/work/s01e01.utterances.json \\
        dub/stems/htdemucs/s01e01.audio --all
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

MELS = 96
HOP = 512

# Context drawn around a case's own span, so the spectrogram shows the
# collision starting and ending rather than cropping straight to it.
PLOT_MARGIN = 2.0

# How far from the case a speaker's own solo line may sit and still count as
# a reference for where their voice usually sits in this scene. Past this a
# line belongs to a different moment in the episode, not this one.
REFERENCE_WINDOW = 15.0
REFERENCE_LIMIT = 3

# A reference clip shorter than this is too little audio to trust a pan
# estimate from.
MIN_REFERENCE = 0.25

TAB10 = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
         "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

# A measured solo pan under this is noise, not a mixing decision - every
# reference clip on this show has come back within a few thousandths of 0.
# Above it, something was actually panned and the case needs a human or an
# LLM to look at the picture rather than trust a rule.
AUTO_RESOLVE_THRESHOLD = 0.15

# Spread used when auto-resolving a case with no real stereo image to match.
# Ordered from the widest positions inward, so the busiest overlaps (handled
# first by the greedy coloring below) get the most separation.
CONFLICT_PALETTE = [-0.6, 0.6, -0.35, 0.35, -0.15, 0.15, 0.0]


def timestamp(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def load_utterances(path):
    utterances = json.loads(Path(path).read_text())
    for utterance in utterances:
        utterance.setdefault("overdub", None)
    return utterances


def cases_in(utterances):
    grouped = {}
    for utterance in utterances:
        if utterance["overdub"] is not None:
            grouped.setdefault(utterance["overdub"], []).append(utterance)
    return {case_id: sorted(group, key=lambda u: u["start"])
            for case_id, group in grouped.items()}


def gap_to_case(utterance, case_start, case_end):
    if utterance["end"] <= case_start:
        return case_start - utterance["end"]
    if utterance["start"] >= case_end:
        return utterance["start"] - case_end
    return 0.0


def conflict_spread(group):
    """Pan each speaker in a case apart from whoever they actually overlap.

    There is nothing in the original mix to match on this show, so the goal
    is intelligibility: whoever genuinely talks over whom should end up
    furthest apart. This is graph colouring — nodes are speakers, edges are
    "these two have overlapping lines in this case" — coloured greedily in
    order of how many others each speaker conflicts with, so the busiest
    speaker is placed first and gets first pick of the widest positions.
    Two speakers who never actually overlap can freely share a side; nothing
    here requires every member of a case to differ from every other.
    """
    speakers = sorted({u["speaker"] for u in group})
    conflicts = {name: set() for name in speakers}
    for a in group:
        for b in group:
            if a["speaker"] != b["speaker"] and a["start"] < b["end"] and b["start"] < a["end"]:
                conflicts[a["speaker"]].add(b["speaker"])

    order = sorted(speakers, key=lambda name: -len(conflicts[name]))
    pan = {}
    for name in order:
        taken = {pan[other] for other in conflicts[name] if other in pan}
        pan[name] = next((p for p in CONFLICT_PALETTE if p not in taken),
                         CONFLICT_PALETTE[-1])
    return pan


def solo_references(utterances, speaker, case_start, case_end):
    """A speaker's own nearby lines, outside any overdub case, closest first."""
    candidates = [u for u in utterances if u["speaker"] == speaker
                 and not u["group"] and u["overdub"] is None
                 and u["end"] - u["start"] >= MIN_REFERENCE]
    scored = [(gap_to_case(u, case_start, case_end), u) for u in candidates]
    scored = [(gap, u) for gap, u in scored if gap <= REFERENCE_WINDOW]
    scored.sort(key=lambda item: item[0])
    return [u for _, u in scored[:REFERENCE_LIMIT]]


def clip_pan_loudness(stereo, rate, start, end):
    """Broadband pan and loudness of one stretch of the stereo vocals stem.

    Pan is the interaural level difference in power: -1 is hard left, +1 hard
    right, 0 is centre. This is a single scalar over the whole clip, on
    purpose — it is the summary number for the dot plot and the JSON
    proposal. The spectrogram panel is where the per-band detail belongs.
    """
    head, tail = int(start * rate), int(end * rate)
    segment = stereo[max(0, head):tail]
    if segment.shape[0] < int(MIN_REFERENCE * rate):
        return None
    left = float(np.mean(np.square(segment[:, 0])))
    right = float(np.mean(np.square(segment[:, 1])))
    total = left + right
    pan = (right - left) / total if total > 0 else 0.0
    loudness_db = float(10 * np.log10(total / 2 + 1e-12))
    return {"pan": round(pan, 3), "loudness_db": round(loudness_db, 1)}


def speaker_summary(utterances, context, rate, speaker, case_start, case_end, context_start):
    """Pan and loudness of a speaker's own nearby lines, absolute time in,
    reading out of `context`, which starts at `context_start` in the episode."""
    refs = solo_references(utterances, speaker, case_start, case_end)
    clips = [clip_pan_loudness(context, rate, u["start"] - context_start,
                               u["end"] - context_start) for u in refs]
    clips = [c for c in clips if c is not None]
    if not clips:
        return None
    return {"pan": round(float(np.mean([c["pan"] for c in clips])), 3),
            "loudness_db": round(float(np.mean([c["loudness_db"] for c in clips])), 1),
            "references": len(clips)}


def mel_power(audio, rate):
    import librosa
    return librosa.feature.melspectrogram(y=audio, sr=rate, n_mels=MELS,
                                          hop_length=HOP, fmax=rate / 2)


def pan_spectrogram(stereo, rate):
    """Per-band pan and loudness across the plotted window.

    Power, not amplitude, because the two channels are summed to judge
    loudness and power is what actually adds. `pan` is the same interaural
    balance as the scalar version, computed per mel band per frame instead of
    once over a whole clip, which is what turns it into a picture rather than
    a number.
    """
    left = mel_power(np.ascontiguousarray(stereo[:, 0]), rate)
    right = mel_power(np.ascontiguousarray(stereo[:, 1]), rate)
    total = left + right
    pan = (right - left) / (total + 1e-12)

    import librosa
    loudness_db = librosa.power_to_db(total, ref=np.max, top_db=80)
    return pan, loudness_db


def draw(output, case_id, case_start, case_end, plot_start, plot_end,
        stereo, rate, group, speaker_names, solo_stats, colors):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker
    from matplotlib.colors import Normalize

    pan, loudness_db = pan_spectrogram(stereo, rate)
    frames = pan.shape[1]
    seconds = stereo.shape[0] / rate

    # Loudness drives opacity, not just color, so silence reads as background
    # rather than as a confident claim about where nothing is.
    alpha = np.clip((loudness_db + 80) / 80, 0.0, 1.0)
    cmap = plt.get_cmap("coolwarm")
    rgba = cmap(Normalize(vmin=-1, vmax=1)(pan))
    rgba[..., 3] = alpha

    figure, axes = plt.subplots(
        3, 1, figsize=(13, 8.5),
        gridspec_kw={"height_ratios": [5, 1.4, 1.6]}, facecolor="#808080")

    time_format = matplotlib.ticker.FuncFormatter(lambda value, _: timestamp(value))

    spec_ax, strip_ax, dot_ax = axes
    extent = [plot_start, plot_end, 0, MELS]
    spec_ax.set_facecolor("#808080")
    image = spec_ax.imshow(rgba, origin="lower", aspect="auto", extent=extent)
    spec_ax.set_ylabel("mel band")
    spec_ax.xaxis.set_major_formatter(time_format)
    spec_ax.set_title(f"overdub case {case_id}: {', '.join(speaker_names)}  "
                      f"({timestamp(case_start)}-{timestamp(case_end)})")
    colorbar = figure.colorbar(plt.cm.ScalarMappable(norm=Normalize(-1, 1), cmap=cmap),
                               ax=spec_ax, pad=0.01)
    colorbar.set_label("pan  (left ←  center  → right)")

    # Who is speaking when, in the same time axis as the spectrogram above,
    # so a color region can be read against a name.
    strip_ax.set_facecolor("#808080")
    for utterance in group:
        color = colors[utterance["speaker"]]
        strip_ax.barh(0, utterance["end"] - utterance["start"], left=utterance["start"],
                      height=0.6, color=color, edgecolor="black", linewidth=0.5)
        strip_ax.text((utterance["start"] + utterance["end"]) / 2, 0, utterance["speaker"],
                     ha="center", va="center", fontsize=8, color="white")
    strip_ax.set_xlim(plot_start, plot_end)
    strip_ax.set_yticks([])
    strip_ax.xaxis.set_major_formatter(time_format)
    strip_ax.set_xlabel("time in episode")

    # Where each character's own, un-overlapped voice sits in this scene —
    # the measurement the proposal is drawn from, laid out so a reviewer can
    # see at a glance whether the cast is already spread out or bunched up.
    dot_ax.set_facecolor("#808080")
    for index, speaker in enumerate(speaker_names):
        solo = solo_stats[speaker]
        if solo is None:
            continue
        dot_ax.scatter([solo["pan"]], [index], s=120, color=colors[speaker],
                       edgecolor="black", zorder=3)
        dot_ax.text(solo["pan"], index + 0.28, f"{solo['pan']:+.2f}",
                   ha="center", fontsize=8)
    dot_ax.axvline(0.0, color="white", linestyle=":", linewidth=1)
    dot_ax.set_xlim(-1.05, 1.05)
    dot_ax.set_ylim(-0.6, len(speaker_names) - 0.4)
    dot_ax.set_yticks(range(len(speaker_names)))
    dot_ax.set_yticklabels(speaker_names)
    dot_ax.set_xlabel("solo pan (measured from each speaker's nearby unmixed lines)")
    dot_ax.invert_yaxis()

    figure.tight_layout()
    figure.savefig(output, dpi=115, facecolor=figure.get_facecolor())
    plt.close(figure)
    return frames, seconds


def analyze_case(case_id, group, utterances, stems, output, margin):
    stem_path = Path(stems, "vocals.wav")
    info = sf.info(str(stem_path))
    rate = info.samplerate
    duration = info.frames / rate

    case_start = group[0]["start"]
    case_end = max(u["end"] for u in group)
    plot_start = max(0.0, case_start - margin)
    plot_end = min(duration, case_end + margin)

    # Read wide enough to cover both the plotted window and any solo
    # reference line a speaker_summary() lookup might reach for, so a
    # reference just outside the plot margin is not read out of bounds.
    context_start = max(0.0, min(plot_start, case_start - REFERENCE_WINDOW))
    context_end = min(duration, max(plot_end, case_end + REFERENCE_WINDOW))
    start_frame = int(context_start * rate)
    frames = int((context_end - context_start) * rate)
    context, _ = sf.read(str(stem_path), frames=frames, start=start_frame,
                        dtype="float32", always_2d=True)
    if context.shape[1] < 2:
        raise SystemExit(f"{stem_path} is mono; there is no stereo image to place against")

    speaker_names = sorted({u["speaker"] for u in group})
    colors = {name: TAB10[index % len(TAB10)] for index, name in enumerate(speaker_names)}

    solo = {name: speaker_summary(utterances, context, rate, name,
                                  case_start, case_end, context_start)
           for name in speaker_names}

    plot_stereo = context[int((plot_start - context_start) * rate):
                          int((plot_end - context_start) * rate)]
    draw(output, case_id, case_start, case_end, plot_start, plot_end,
        plot_stereo, rate, group, speaker_names, solo, colors)

    proposed = {}
    for name in speaker_names:
        if solo[name] is not None:
            proposed[name] = {"pan": solo[name]["pan"], "gain_db": 0.0}
        else:
            proposed[name] = {"pan": 0.0, "gain_db": 0.0}

    return {
        "span": [round(case_start, 3), round(case_end, 3)],
        "speakers": speaker_names,
        "utterances": [u["id"] for u in group],
        "solo": solo,
        "proposed": proposed,
        "image": str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("utterances")
    parser.add_argument("stems", help="Demucs stem directory holding vocals.wav")
    parser.add_argument("--case", type=int, help="analyze one case by id")
    parser.add_argument("--all", action="store_true", help="analyze every case found")
    parser.add_argument("-o", "--output", help="PNG to write (only with --case)")
    parser.add_argument("--overdubs", help="resolution JSON to update "
                        "(default beside the utterances)")
    parser.add_argument("--margin", type=float, default=PLOT_MARGIN,
                        help=f"seconds of context to plot around the case (default {PLOT_MARGIN})")
    parser.add_argument("--auto-resolve", action="store_true",
                        help="resolve a case automatically, by spreading conflicting "
                             "speakers apart, when every measured solo pan is within "
                             f"{AUTO_RESOLVE_THRESHOLD} of centre (i.e. there is nothing "
                             "in the original mix to match). Anything measuring a real "
                             "pan is left for manual review either way.")
    args = parser.parse_args()

    if not args.case and not args.all:
        raise SystemExit("pass --case N or --all")
    if args.output and args.all:
        raise SystemExit("--output names one file; use --all without it")

    utterances_path = Path(args.utterances)
    utterances = load_utterances(utterances_path)
    cases = cases_in(utterances)
    if not cases:
        raise SystemExit("no overdub cases in this utterance list "
                         "(run dub_script.py again to detect them)")

    wanted = sorted(cases) if args.all else [args.case]
    missing = [case_id for case_id in wanted if case_id not in cases]
    if missing:
        raise SystemExit(f"no such case(s): {missing}; known cases are {sorted(cases)}")

    slug = utterances_path.name.replace(".utterances.json", "")
    overdubs_path = Path(args.overdubs) if args.overdubs else Path(
        str(utterances_path).replace(".utterances.json", ".overdubs.json"))
    resolutions = json.loads(overdubs_path.read_text()) if overdubs_path.exists() else {}

    for case_id in wanted:
        group = cases[case_id]
        output = Path(args.output) if args.output else Path(
            utterances_path.parent.parent, "preview", f"{slug}.overdub_{case_id}.png")
        output.parent.mkdir(parents=True, exist_ok=True)

        entry = analyze_case(case_id, group, utterances, args.stems, output, args.margin)
        existing = resolutions.get(str(case_id), {})
        entry["resolved"] = existing.get("resolved")
        entry["status"] = existing.get("status", "proposed")
        entry["notes"] = existing.get("notes", "")

        auto_eligible = all(solo is None or abs(solo["pan"]) <= AUTO_RESOLVE_THRESHOLD
                            for solo in entry["solo"].values())
        auto_applied = False
        if args.auto_resolve and entry["status"] != "resolved" and auto_eligible:
            spread = conflict_spread(group)
            entry["resolved"] = {name: {"pan": round(pan, 3), "gain_db": 0.0}
                                 for name, pan in spread.items()}
            entry["status"] = "resolved"
            entry["notes"] = (f"Auto-resolved: every measured solo pan was within "
                              f"{AUTO_RESOLVE_THRESHOLD} of centre, so there is nothing "
                              f"in the original mix to match here. Speakers spread apart "
                              f"by who actually overlaps whom in this case.")
            auto_applied = True

        resolutions[str(case_id)] = entry

        print(f"case {case_id}  {timestamp(entry['span'][0])}-{timestamp(entry['span'][1])}"
              f"  {', '.join(entry['speakers'])}")
        for name in entry["speakers"]:
            solo = entry["solo"][name]
            if solo is None:
                print(f"    {name:<14} no solo reference within {REFERENCE_WINDOW:.0f}s "
                      f"- proposed pan left at 0.0, needs a judgment call")
            else:
                print(f"    {name:<14} solo pan {solo['pan']:+.2f}  "
                      f"{solo['loudness_db']:.1f} dB  ({solo['references']} ref lines)")
        if auto_applied:
            print(f"    auto-resolved: {entry['resolved']}")
        elif not auto_eligible:
            print(f"    NOT auto-eligible - a measured solo pan exceeds "
                  f"{AUTO_RESOLVE_THRESHOLD}, needs a look")
        print(f"    wrote {output}")
        if entry["status"] == "resolved":
            print(f"    already resolved: {entry['resolved']}  (re-analysis left it in place)")

    overdubs_path.write_text(json.dumps(resolutions, indent=1))
    unresolved = sum(1 for entry in resolutions.values() if entry["status"] != "resolved")
    print(f"\nwrote {overdubs_path}")
    if unresolved:
        print(f"{unresolved} case(s) still need a resolved pan/gain before "
              f"dub_render.py will place them off-centre")

    return 0


if __name__ == "__main__":
    sys.exit(main())
