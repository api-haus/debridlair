# debridlair — agent operating guide

This is the operating manual for whoever runs this stack, and that is normally
you, the agent. The user talks; you fetch, file, and fix. Emby 4.8.11 on
`:8096` (admin: see `.env`), Prowlarr on `:9696`, all state in this directory.
Human-facing overview in `README.md`; secrets in `.env` (chmod 600, never
commit, never print).

@PREFS.md

The user's standing preferences live in `PREFS.md` — audio and subtitle
policy, size ceilings, how complete a series request is, how much to ask
before acting. **It is gitignored and it overrides this file on any
conflict.** When the user tells you to change a preference, edit `PREFS.md`,
not this file — this file is shared doctrine and must not pick up one person's
settings. If `PREFS.md` is missing, copy `PREFS.example.md` to it first.

Write to `PREFS.md` only on an explicit instruction. It is not a place to
record what the user watches or to log inferences about their taste.

## Default to fetching — don't ask first

Acquiring content is the whole point of this stack. When a specific movie or
show title is on the table — the user names it, asks for it, or is reacting
to a recommendation (yours or theirs) in a way that signals they want it —
just run `torbox_find.py` and queue it. **Don't ask "want me to fetch this?"
first; that's the default action, not a question.** Only hold off when the
user is clearly just chatting or researching with no acquisition intent, or
has explicitly said to wait. Fetching is cheap and reversible (see Torbox
deletes below), so bias toward doing it over asking permission — this
applies just as much to titles you yourself suggested a moment ago as to
ones the user named.

`PREFS.md` sets how far this goes for this user; if it asks for more
confirmation than the above, follow it.

## Check Emby before queuing

"Don't ask first" means don't ask the *user* — it does not mean skip checking
the library. Before running `torbox_find.py`, always check whether the title
(and the specific edition asked for — Extended/Director's Cut vs theatrical)
is already there:

```bash
curl -s "http://localhost:8096/emby/Items?api_key=$(cat sync-state/emby_api_key)&searchTerm=TITLE&Recursive=true&Fields=Path&IncludeItemTypes=Movie,Series"
```

If a matching copy already exists, don't queue a duplicate — different
release-name spellings (colon vs dash, spacing, brackets) won't get merged by
the sync's dedupe, so a redundant fetch shows up as a second entry in Emby,
not a clean replace. Only fetch when nothing matches, or when the existing
copy is a worse edition than what's being asked for (and in that case, delete
the old torrent per the Torbox-deletes rule below once the new one lands).

## Add a movie or series by name (the common request)

```bash
cd /mnt/archive4/DEBRID
python3 scripts/torbox_find.py "Sinners 2025"          # movies: auto-pick best release
python3 scripts/torbox_find.py "The Pitt S01" --tv     # series: pass --tv
python3 scripts/torbox_find.py "Title" --list          # show top 10 releases first
python3 scripts/torbox_find.py "Title" -n 3            # queue a specific pick
```

How complete a series request is — one season or the whole show with specials
— is set in `PREFS.md`. When it asks for the complete show, search the title
with `S00`, `featurettes`, `extras`, `bonus`, and `documentary` (TV category)
as well, and queue the streamable results the same way. If no extras are
indexed, note that and move on.

Release quality and language preferences are also in `PREFS.md` — read them
before picking, because the auto-pick does not enforce all of them (watch for
MVO/DVO/Rus dubs in Knaben results). Then fast-track instead of waiting for
the 15-min loop:

```bash
python3 scripts/torbox_sync.py
curl -s -X POST "http://localhost:8096/emby/Library/Refresh?api_key=$(cat sync-state/emby_api_key)"
python3 scripts/emby_probe.py --limit 10
```

Verify it landed:

```bash
curl -s "http://localhost:8096/emby/Items?api_key=$(cat sync-state/emby_api_key)&searchTerm=TITLE&Recursive=true&Fields=Path"
```

If metadata didn't match (year None, wrong title), force it:
`POST /emby/Items/{id}/Refresh?MetadataRefreshMode=FullRefresh` — or in the
UI use Identify. Item GET by id is `/emby/Users/{uid}/Items/{id}`, not
`/emby/Items/{id}`.

## Keeping an airing show topped up

`sync-state/watchlist.txt` lists shows to follow, one per line as
`<library/tv folder> | <query template>`:

```
Chainsmoker Cat | Chainsmoker Cat S{season:02d}E{ep:02d}
```

`scripts/torbox_watch.py` counts from the highest episode already in the
folder's newest season and queues only what comes after, so it never
re-queues. It runs hourly (every 4th `torbox-sync` loop); run it by hand with
`python3 scripts/torbox_watch.py`. Prefer `{season:02d}` over a hard-coded
season — that is what lets it also catch a *new* season premiering. Add a
line whenever the user asks to "watch" or follow an ongoing show, and drop
one when a show ends.

## Hard rules

- **Never edit `library/` by hand.** It is generated by
  `scripts/torbox_sync.py`; the 15-min `torbox-sync` container loop will
  create/prune files to match Torbox. Change the script, then run it.
- **Don't `DELETE /Library/VirtualFolders`** on this Emby build — it 500s.
- Emby is pinned to 4.8.11 (4.9 scan regressions + StrmAssistant compat).
- The **whole stack** shares one 40 Mbit download cap: a `tc tbf` on the
  `br-debrid` bridge, applied by `emby-throttle` (`scripts/throttle.sh`,
  `RATE`/`BRIDGE` env in compose). Re-apply with
  `docker compose up -d emby-throttle`; check with
  `tc -s qdisc show dev br-debrid`. Shaping the bridge rather than each veth
  is deliberate — per-veth caps give every container its own 40 Mbit.
- **Prefer `docker compose stop` over `down`.** `down` removes the rclone
  container without unmounting, leaving a dead FUSE endpoint at `rclone/mnt`
  that makes the next `up` fail with `transport endpoint is not connected` —
  and clearing it needs root (`sudo umount -l rclone/mnt`). If you must
  `down`, unmount first.
- Only acquire content through `torbox_find.py` — it enforces the
  streamability limits that match the 40 Mbit cap, and the language policy, at
  acquisition time (the ceilings themselves are `EP_MAX`/`MOVIE_MAX`/`PACK_MAX`
  at the top of that script, and `PREFS.md` is what says what they should be).
  Do not bypass with `--allow-fat` or raw magnets unless the user explicitly
  asks.
- `torbox_find.py` enforces only part of `PREFS.md`. Its auto-pick ranks by
  resolution/remux/HDR/seeders and does **not** check subtitle language, so a
  release that violates a preference can still win. Whatever the script does
  not enforce, you enforce — requeue a compliant release and drop the other
  rather than handing the user a choice they have already made in `PREFS.md`.
- Torbox deletes: `POST /v1/api/torrents/controltorrent`
  `{"torrent_id": N, "operation": "delete"}` — irreversible. Do not ask first;
  delete and tell the user what was dropped and why, right after.
- `.strm` layout is normalized by the sync script (show/season parsing,
  quality dedupe for TV). If Emby shows strays or duplicate episodes, the fix
  belongs in `torbox_sync.py`, not in Emby or on disk.
- **The sync only prunes when it got a complete account listing.** Torbox's
  API drops handshakes and serves sporadic 403/520s; a source that fails to
  list makes every `.strm` it backs look stale. `torbox_sync.py` retries, then
  skips the prune entirely if any source errored, and refuses to delete >25%
  of a populated library without `--allow-mass-prune`. Don't "simplify" these
  guards away — without them one timed-out request wipes the library (it
  deleted 1137 files on 2026-08-08). A `[skip-prune]` line on stderr is the
  guard working; re-run once the API is healthy.

## Stack layout

- `docker-compose.yml` — emby, torbox-mount (rclone WebDAV FUSE),
  torbox-sync (15-min loop), prowlarr
- `scripts/` — torbox_sync.py, torbox_find.py, torbox_add.py, emby_setup.py,
  emby_probe.py
- `sync-state/emby_api_key` — Emby API key
- Skip-intro pipeline: StrmAssistant fingerprint task daily 03:47, native
  Detect Episode Intros 05:33. New episodes get markers within ~a day.
