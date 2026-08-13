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

If there is no `PREFS.md`, mention `/hello-debrid` once — lightly, at a natural
pause, never as a gate on the thing they asked for. *"You have no PREFS.md yet;
`/hello-debrid` sets your language, size and autonomy preferences in about a
minute."* If they opened the session asking for a film, fetch the film first and
mention it after. Do not ask the preference questions inline instead: that is
what the skill is for, and asked ad hoc they end up answered but unrecorded.

## First run — setting the stack up

Someone opening a session here with nothing running yet is a normal request, not
a special occasion. `docker compose ps` tells you where you are. Work through
this and ask only for what you cannot know.

1. **Credentials.** Copy `.env.example` to `.env`, `chmod 600` it, and ask the
   user for the Torbox API key, their Torbox account email and password, an Emby
   admin user and password, and an Emby Premiere key if they have one (without
   it there is no Skip Intro and no hardware transcoding, but the stack runs).
   The rclone password is the account password put through
   `rclone obscure "$TORBOX_PASSWORD"` — the WebDAV mount authenticates with the
   account email and password, *not* the API key.
2. **Host prerequisites.** Docker with `/dev/fuse`; `GIDLIST` in
   `docker-compose.yml` is this box's render and video group ids
   (`getent group render video`); the parent of this directory must be a shared
   mount (`findmnt -no PROPAGATION .`) or Emby's `rshared` bind fails; drop the
   `devices:` entry from the `emby` service if there is no VAAPI `/dev/dri`.
3. `docker compose up -d`
4. `python3 scripts/torbox_sync.py` — first library build from whatever the
   account already holds.
5. `python3 scripts/emby_setup.py` — startup wizard, admin user, Premiere key,
   API key into `sync-state/emby_api_key`, libraries, intro detection,
   StrmAssistant. Safe to re-run.
6. `docker compose restart emby` — loads StrmAssistant.
7. **Prowlarr indexers.** Not scriptable: the indexer list lives in
   `prowlarr/prowlarr.db`, which is not committed. Walk the user through adding
   Knaben, The Pirate Bay, LimeTorrents and Nyaa.si at `:9696`, or
   `torbox_find.py` returns nothing. (1337x and EZTV are Cloudflare-walled and
   need a FlareSolverr sidecar.)
8. **Emby playback defaults.** `PlayDefaultAudioTrack=false` and always-on
   subtitles on the Emby user, matching whatever `PREFS.md` ends up saying. The
   acquisition side is enforced by `torbox_find.py`; this is the playback half
   and it is a UI setting.
9. Run `/hello-debrid` to write `PREFS.md` before fetching anything.

Then ask them what they want to watch.

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

A bare title and nothing else — just the name, no question mark, no other
sentence around it — **is** naming it. Don't treat a one-line title-only
message as too thin to act on and stop to ask what they meant; that message
shape is exactly the case this rule exists for, not an exception to it.

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
`<library/tv folder> | <query template>`. It is gitignored, like `PREFS.md`
and for the same reason — what somebody follows is theirs, not shared
doctrine. `sync-state/watchlist.example.txt` is the tracked template; copy it
if there is no watchlist yet.

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

## Dubbing a show that was never dubbed

Some shows only ever got subs. `scripts/dub_*.py` builds an English track for
one locally, cloning each character from their own performance and keeping the
original music and effects. Full guide in `docs/dubbing.md`.

**This box may not be able to do it, and on a box that cannot, the feature does
not exist.** Run the check before dubbing, and before so much as mentioning
that dubbing is possible:

```bash
python3 scripts/dub_check.py        # exit 0 can dub, exit 1 cannot
```

It needs a CUDA GPU with 8 GB of VRAM and 40 GB free. If it says no, say
nothing: do not offer dubbing, do not describe it as something to enable later,
do not suggest a cloud GPU. Bringing it up on hardware that cannot run it only
sells the user a setup that will not finish. Answer what they actually asked
and leave it there. Mention it only if they ask about dubbing themselves, and
then say plainly that this machine cannot.

Where the check passes, dubbing is a normal offer — but it still costs about a
quarter hour of GPU per episode, so agree the scope before starting a season.

Then check the show itself. The pipeline speaks the release's own English
subtitles, so it needs a fansub that labels each line with its character:

```bash
python3 scripts/dub_survey.py --mkv EPISODE.mkv
```

A main cast reading `usable clone` or better gets the full dub, one cloned
voice per character.

A track that names nobody cannot be cast, and the survey says so instead of
printing an empty table. That is not a reject: it gets a solo read — one actor
for the whole episode, the amateur-dub form — through `--solo` on the same
tools. Offer that rather than declining, and do not reach for diarization.
`docs/dubbing.md` has both paths.

Where a labelled fansub exists for a *different* release, `scripts/dub_align.py`
fits the timing offset between the two before anything else runs. A subtitle
track that is a second out dubs the whole episode over the wrong shots and
every later stage accepts it happily.

Dubbing never writes to `library/`. It works on local copies under `dub/`, one
show per working directory (`--work dub/<show>`) — episodes are named by
season and episode number, so two shows in one directory overwrite each other.
Finished episodes belong in `dub/finished/tv/<Show>/Season NN/`, which Emby
serves as its own "TV Shows (Dub)" library; a render left anywhere else under
`dub/` is not in any library.

**A whole season goes through `scripts/dub_season.py`, never a shell loop over
`dub_render.py`.** It renders one episode at a time, named by the show's title
or any of its aliases, and stops and resumes without losing more than the line
being spoken. `/dub` drives it; the skill is in `.agents/skills/dub/`.

```bash
python3 scripts/dub_season.py --status                  # every prepared season
python3 scripts/dub_season.py "Shirokuma Cafe"          # run, or carry on
python3 scripts/dub_season.py --halt                    # stop whatever is going
```

To hand a finished dub to somebody, `scripts/dub_share.py <show> <episode>`
writes an MP4 to `dub/share/` carrying the dub track only. Do not send a file
out of `library/` or `dub/finished/` directly — those are Matroska with two
audio tracks, which chat clients attach rather than play and which leave the
recipient a coin toss over the language.

Where it got to is measured off the disk, never recorded, so a session that
knows nothing about an earlier one resumes correctly by running the same
command. `--halt` writes `PAUSE` in the show's work directory, which any
session can set and which stops the render between lines; running the tool
again clears it. When the user asks to pause a season, `--halt` is the
answer — do not kill the process, and never delete a `clips/` directory to
tidy up, because that is the resume point. Full detail in `docs/dubbing.md`.

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

## Agent configuration

Durable agent configuration stays provider-neutral. No agent is the one this
repo is built for.

- Shared guidance lives in this file. `CLAUDE.md` only `@`-imports it, because
  Claude Code looks for that name; it holds nothing but Claude-specific notes.
- Shared assets — skills, and anything reusable added later — live under
  `.agents/`. That copy is the source of truth.
- `.claude/skills/` is an adapter surface: a mirror of `.agents/skills/`, kept
  in the repo so a Claude Code user has the skills on clone with no setup step.
  It is a copy, never the original. Edit `.agents/`, then re-mirror:

  ```bash
  rsync -a --delete .agents/skills/ .claude/skills/
  ```

- `.claude/settings.json` is tracked as well. It pins the model, so a clone
  needs no setup step for that either. Everything else under `.claude/` is
  local agent state and is gitignored.
- A provider-specific file only wins for that provider's surface. This file
  wins everywhere else, and `PREFS.md` wins over this file (see the top).
