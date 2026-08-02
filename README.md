# DEBRID — Emby + Torbox streaming stack

Fully automated Emby media server streaming directly from Torbox, with
native Skip Intro (intro/credits detection) working over STRM.

## Components (docker-compose)

| Service | Image | Purpose |
|---|---|---|
| `torbox-mount` | `rclone/rclone` | FUSE mount of Torbox WebDAV at `./rclone/mnt` → Emby `/media/torbox` (read-only, full VFS cache in `./rclone/cache`, 80 GB cap) |
| `emby` | `emby/embyserver:4.8.11.0` | Media server on `:8096` (`:8920` https). Config in `./emby`. HW transcoding via `/dev/dri` (VAAPI, Premiere) |
| `torbox-sync` | `python:3.12-alpine` | Loop (15 min): sync Torbox downloads → `.strm` files, refresh Emby library, probe new items |
| `prowlarr` | `linuxserver/prowlarr` | Indexer aggregation/search on `:9696` (config in `./prowlarr`) |
| `emby-throttle` | `alpine` (one-shot) | Applies a `tc tbf` cap (`RATE`, default 40 Mbit) on the `br-debrid` bridge — **one shared budget for every container**, not 40 Mbit each. Re-runs on every `docker compose up` |

## Libraries

- **TV Shows** → `./library/tv` — generated `.strm` from all Torbox torrents/usenet/web downloads
- **Movies** → `./library/movies` — same
- **Anime** → `/mnt/archive4/strm-extra/anime` (pre-existing STRM collection)
- **Torbox (raw)** → the WebDAV mount itself (browse everything as files)

`.strm` layout is **normalized**, not torrent-shaped: the sync parses
show/season/episode out of release names (`SxxExx`, `7x01`, `SS.EE - title`,
anime `Show - 03 [1080p]`, `S002E062`) and files everything as
`Show Name (Year)/Season 01/….strm`, stripping site prefixes, `[group]` tags
and quality junk. Single-episode torrents therefore merge into the proper
series and sample files are skipped. When the same episode exists in
multiple releases, only the best copy is kept (resolution → remux/blu-ray →
HDR); Emby 4.8 does not version-group episodes, so duplicates are dropped
instead. Movies land in clean `Title (Year)/` dirs; bonus material of
multi-file releases stays inside the parent movie folder. Dirs differing
only by case or a `(year)` suffix are merged automatically.

## Adding content

Content discovery runs through the **Prowlarr** container (`:9696`, indexers:
Knaben, The Pirate Bay, LimeTorrents, Nyaa.si — 1337x/EZTV are Cloudflare-walled
and would need a FlareSolverr sidecar). The finder script searches, ranks
(resolution → remux → HDR → seeders), resolves the magnet, and queues it:

```bash
python3 scripts/torbox_find.py "Sinners 2025"          # auto-pick best release
python3 scripts/torbox_find.py "The Pitt S01" --tv     # TV categories
python3 scripts/torbox_find.py "Sinners 2025" --list   # show top 10
python3 scripts/torbox_find.py "Sinners 2025" -n 3     # queue pick #3
python3 scripts/torbox_add.py "magnet:?xt=urn:btih:..."   # or a magnet/.torrent directly
```

Once Torbox caches it, the 15-min sync loop writes the `.strm`, Emby scans it
in and the probe loop forces stream probing — no manual steps.

**Streamability limits are enforced at acquisition time** (because the emby
container is throttled to 40 Mbit): `torbox_find.py` refuses remuxes and
releases over ~12 GB per episode / ~30 GB per movie / ~80 GB per season pack
(`--allow-fat` overrides). The sync's TV dedupe also ranks remuxes last, so
when duplicates exist the streamable copy wins.

**Language policy: original audio only, English subtitles.** Dub-only
releases (Spanish/Latino/Castilian, Russian MVO/DVO voice-overs, etc.) are
refused by `torbox_find.py`; dual/dub+original releases are tolerated but
ranked below plain original-audio ones. The Emby user is configured for
original audio (`PlayDefaultAudioTrack=false`) + English subtitles always on,
and the TV dedupe prefers copies without dub-only language tags.

## Skip Intro (how it works)

1. Emby Premiere is registered; TV libraries have *Detect intros* markers
   enabled (`EnableMarkerDetection` + during library scan).
2. Emby only lazy-probes `.strm` at playback time, so
   `scripts/emby_probe.py` requests `PlaybackInfo` for every unprobed item —
   this makes Emby probe the remote URL without playing it.
3. The **StrmAssistant** plugin (`emby/plugins/StrmAssistant.dll`,
   config in `emby/plugins/configurations/Strm Assistant*.json`) unlocks
   Emby's built-in intro detection for STRM: its *Extract Intro Fingerprint*
   scheduled task fingerprints the first 10 min of each episode's audio
   directly over the Torbox URL.
4. The native *Detect Episode Intros* task then matches fingerprints per
   season and writes intro markers → Skip Intro button / auto-skip in clients.

## Scripts (`./scripts`)

- `torbox_sync.py` — Torbox API → `.strm` library (idempotent, prunes stale,
  normalizes release names into Emby-friendly `Show/Season NN/` layout)
- `torbox_add.py` — queue a magnet/.torrent into Torbox from the CLI
- `torbox_find.py` — search indexers via Prowlarr and queue the best release
- `emby_setup.py` — full server bootstrap: startup wizard, admin user,
  Premiere key, API key (`sync-state/emby_api_key`), libraries, intro
  detection, StrmAssistant install. Safe to re-run.
- `emby_probe.py` — force lazy-probe of unprobed STRM items
  (`--limit N`, `--series Name`)

## Rebuild from scratch

```bash
cp .env.example .env          # fill in; obscured pass = rclone obscure "$TORBOX_PASSWORD"
docker compose up -d
python3 scripts/torbox_sync.py
python3 scripts/emby_setup.py
docker compose restart emby   # load StrmAssistant
python3 scripts/emby_probe.py # initial probe pass (optional, sync loop does it)
```

Then in Emby dashboard run *Extract Intro Fingerprint* (Strm Assistant)
and *Detect Episode Intros* — or wait for their schedules.

**Host prerequisites:** Docker + `/dev/fuse`; a VAAPI-capable `/dev/dri` (or
drop the `devices:` entry from `emby`); the parent of this directory must be a
**shared** mount (`findmnt -no PROPAGATION .`) or emby's `rshared` bind fails.

**Not reproduced by these scripts — do these by hand on a fresh box:**

1. **Prowlarr indexers.** `torbox_find.py` reads the API key out of
   `prowlarr/config.xml` (auto-generated), but the indexers themselves live in
   `prowlarr/prowlarr.db`, which is not committed. Add Knaben, The Pirate Bay,
   LimeTorrents and Nyaa.si in the Prowlarr UI (`:9696`) or the finder returns
   nothing.
2. **Audio/subtitle policy.** Set `PlayDefaultAudioTrack=false` and
   always-on English subtitles on the Emby user — the original-audio policy is
   enforced at acquisition time by `torbox_find.py`, but playback defaults are
   a UI setting.
3. **Host-specific compose values.** `GIDLIST` is this box's render/video group
   IDs (`getent group render video`); the `/mnt/archive4/strm-extra/anime` bind
   and its matching Anime library in `emby_setup.py:236` are local to this
   machine — drop both if that path doesn't exist.

## Notes / gotchas encountered

- **Emby 4.8.11.0 is pinned.** Emby 4.9.5 has scanning regressions
  (libraries scanning to empty), and StrmAssistant community edition
  supports 4.8.5–4.8.11 only.
- **Collection type must be `tvshows`** when creating TV libraries via API.
  `tv` is silently accepted but produces a library that never imports
  episodes.
- Torbox WebDAV auth = account email + account password (not API key).
  rclone password is stored obscured in `.env`.
- StrmAssistant reads its options from
  `plugins/configurations/Strm Assistant.json` and
  `Strm Assistant_IntroSkipOptions.json` (found empirically via inotify).
- Secrets live in `.env` (chmod 600). Do not commit.
