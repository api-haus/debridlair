# How `torbox_sync.py` builds the library

`scripts/torbox_sync.py` turns a Torbox account listing into a tree of `.strm`
files that Emby serves. Every rule below exists because a real release broke
the obvious version of the code. This page holds the reasons; the script keeps
one-line pointers back here.

The script never asks Torbox what a release *is*. It reads file names, sizes
and folder shapes, and decides. That is why the rules read like a list of
special cases: they are.

## Three destinations

An item lands in exactly one of `library/tv`, `library/movies` or
`library/music`. The choice is made per item, not per file:

- an item with at least one qualifying **video** file is a video item, and its
  audio files are ignored (they are the OST folder bundled with an anime batch,
  not an album someone asked for);
- an item with **no** video file and at least one audio file is a music item;
- an item with neither is skipped.

A "qualifying" file clears `MIN_VIDEO_SIZE` / `MIN_AUDIO_SIZE` and is not a
sample. Below those sizes a file is a broken upload or a rip artefact.

## Episode numbering

### An explicit `E` beats a bare number

`ANIME_EP` reads absolute numbering — `[Group] Show Name - 03 [1080p]`. A bare
number needs two digits before it counts as an episode, because a one-digit
number that late in a name is usually part of the title. An explicit `E`
prefix is unambiguous, so `- E1 v2` counts on one digit.

### Season comes from the folder when the filename has none

`FOLDER_SEASON` reads a season out of the immediate parent folder
(`Attack on Titan Season 2`). A batch with one absolute-numbered subfolder per
season carries no season in the filename at all — `Show - 26.mkv` — so without
the folder hint every season collides into `Season 01`.

The episode number in that shape is still a whole-series absolute count. The
`renumber_key` returned by `tv_target()` marks those files, and `collect()`
resets them to a 1-based in-season sequence once every file in the item has
been classified.

### Underscores hide every anchor

Some groups separate every token with underscores rather than spaces or dots:
`[Cleo]Shinsekai_yori_-_01_(...)`. Underscore is a word character, so the `\s`
and `\b` anchors in the episode patterns never fire and the whole series is
misread as a pile of movies. `tv_target()` matches against a probe copy with
underscores substituted for spaces. The substitution is 1:1, so every match
offset stays valid against the original filename.

### OVAs and specials are season 0

`SPECIALS_SUFFIX` folds `<Show> OVA` and `<Show> Specials` into season 0 of the
parent show. That is Emby's own convention, and it is the only way the bonus
episodes inherit the parent's artwork and metadata instead of appearing as an
unidentifiable extra entry beside it.

## Movies

`MAIN_FILE_MIN_SIZE` is the "this is a real movie" test. A bonus clip can
legitimately run to a few hundred MB — a lossless anime clean-ED, a making-of
documentary — but nothing in this library's actual bonus content approaches a
feature's size, so size is a far more reliable signal than the file's name.

Every file clearing that bar counts, not only the largest. A franchise batch
bundles several real movies of similar size (four recap movies, no year in any
of their names), and picking only the biggest dropped the rest as if they were
bonus clips.

Movie paths are always flattened to `movie_dir/file` or
`movie_dir/Featurettes/file`. Any preserved release subfolder — a site prefix,
a shared collection wrapper, an uploader tag — risks Emby reading it as the
title, and it orphans bonus clips with no sibling movie file whenever a
different release wins the quality dedupe.

## Music

Music releases name themselves `Artist - Album Year(FLAC)`, and every tracker
spells that differently. `split_artist_album()` strips format and rip junk
(`(FLAC)`, `[24bit-96kHz]`, `_Scans`, `[EAC-LOG-CUE]`), splits artist from
album on the first spaced dash, and pulls a four-digit year out of what is
left. A bare year in the artist slot means the name was
`1993 - Third Ear Recitation` and the artist comes from the item instead.

A trailing parenthetical on the artist is a sideman list —
`David S. Ware (Matthew Shipp, William Parker, Whit Dickey)` — and is dropped,
or every quartet becomes its own artist.

The layout is `music/<Artist>/<Album (Year)>/<track>.strm`, plus a `CD N`
level where the release has one. The audio extension is stripped from the
`.strm` name, because that name is the track title Emby shows.

### Emby files music `.strm` as folders, not albums

Measured on Emby 4.8.11: a `.strm` in a music library resolves as an `Audio`
item and direct-plays, but its folder stays a plain `Folder`. It never becomes
a `MusicAlbum`, and no `MusicArtist` is created. A real `.flac` dropped in the
same library resolves as `MusicAlbum` + `MusicArtist` and reads its own tags,
so the difference is the extension: Emby's album resolver looks for a known
audio extension in the folder and `.strm` is not one.

Nothing in the library options changes this, and symlinking the real files is
not available either — Torbox's WebDAV is one flat namespace of basenames, so
`01 - Intro.flac` from two albums is the same path.

What this costs: no album cards, no artist pages, no tags. What still works:
the Artist → Album → tracks folder tree, playback, and direct play.

### `folder.jpg` is the only cover an album gets

Because there are no tags, the sync downloads the release's own front cover
through the Torbox API and writes it as `folder.jpg` in the album directory —
a real local file, a few hundred KB. Emby reads it as the folder's Primary
image, which is what puts a cover on the album in the Music library.

`pick_cover()` prefers a name like `front`/`cover`/`folder`, demotes anything
reading `back`/`tray`/`disc`, and falls back to the largest image under
`MAX_COVER_SIZE`. The prune deletes a `folder.jpg` once the album's last
`.strm` has gone, or the empty album directory would never be removed.

### A CUE image becomes tracks through `cueslice`

A rip stored as one FLAC plus a `.cue` sheet — common for pre-2010 jazz
uploads — has no per-track files to write, and Emby has no CUE support at all:
there is not one `cuesheet` string in any of its DLLs. Left alone, such an
album is a single item of album length.

`scripts/cueslice.py` is a small service in the compose stack that answers a
request for one track by running ffmpeg against the remote file and returning
only that track's audio. `torbox_sync.py` fetches the sheet, and writes one
`.strm` per track pointing at it:

```
01 - Autumn Leaves.strm -> http://cueslice:8099/slice?u=<source>&ss=0&t=223.0
```

Nothing is downloaded, nothing is split, and the source file is never
touched. The sheet also states PERFORMER, TITLE and DATE, which for a
single-file rip is the only place the artist and album are written down —
`music_target()` prefers them over anything parsed from the torrent name.

**It answers in PCM, wrapped as WAV, at the source's own rate and depth.** No
sample is re-encoded. PCM is the point: its length in bytes follows from its
length in seconds, so the service can send an exact `Content-Length` and
honour byte ranges. A piped FLAC could do neither, and Emby would show a track
of unknown duration that cannot be scrubbed.

Measured on *Third Ear Recitation* (59:11, nine tracks): every track's
duration in Emby matches its span in the sheet, a slice is byte-identical to
decoding the source at the same offset, and Emby direct-streams it. Throughput
is 126–174 Mbit/s against the 1.41 Mbit/s playback needs. The cost is the
initial seek — 1.3 s into track 1, 11.9 s into track 9, which sits 50 minutes
into the file — because ffmpeg has to seek that far through a remote FLAC
before the first sample comes out.

Emby proxies the stream rather than handing the URL to the client
(`DirectStreamUrl` points back at Emby), so the service needs no published
port and is reached by its service name on the compose network.

`torbox_find.py --music` still ranks a release whose name says `tracks+.cue`
above one that says `image+.cue`. A real per-track rip needs no service at
all, and it carries its own tags.

## Guards

### The retry in `api_get()` is load-bearing

Torbox's edge routinely drops a TLS handshake or answers 403/520 under load.
A bare failure used to propagate as "this source owns nothing", which the
prune reads as a mandate to delete every `.strm` that source backs.

### The prune only runs on a complete listing

Pruning is safe only when `wanted` is a complete picture of the account. A
source that errored contributes nothing, so every `.strm` it backs looks
stale — one dropped handshake deleted 1137 files on 2026-08-08.

Two guards stop that: any source that raised blocks the prune entirely, and a
prune of more than `MAX_PRUNE_FRACTION` of an already-populated library needs
`--allow-mass-prune`. A truncated 200 looks identical to a real mass delete,
so the caller confirms rather than the script guessing.

A `[skip-prune]` line on stderr is the guard working. Re-run once the API is
healthy.

### Duplicates are dropped, not merged

The same episode cached from two releases shows up twice in Emby. `qscore()`
ranks by resolution, then Blu-ray, then HDR, and penalises a release whose
name carries a non-English language tag with no `eng`/`multi`/`dual` beside
it. Remuxes and AI upscales score negative — a remux cannot stream inside the
40 Mbit cap, and an upscale is a fake resolution.

Movies get the same treatment per title folder. Music gets it per track, where
the only thing that ranks is lossless over lossy.
