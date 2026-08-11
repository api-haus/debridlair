# debridlair

**A private streaming library that your LLM runs for you.**

You have a debrid account and a media server. Between them sits the boring part:
finding a release, judging whether it is the right one, getting it into the
library, fixing the cases where the metadata lands wrong. debridlair is that
middle layer, and it is built to be operated by an agent rather than by you.

There is no web UI to learn and no `*arr` fleet to babysit. You open a coding
agent — Claude Code, Codex, whatever reads an `AGENTS.md` — in this directory
and talk to it.

```
you:  put on something like Sicario but Korean
llm:  A Bittersweet Life, The Man from Nowhere, or New World. Which?
you:  all three
llm:  queued. two are cached already; New World lands in a few minutes.

you:  the wife hates subtitles on kids' stuff, dubs are fine for those
llm:  noted in PREFS.md — dub-only releases still get refused for everything
      else.

you:  what happened to Silo? new episode aired yesterday
llm:  it's in the watchlist but S03E04 wasn't indexed until this morning.
      queued it just now, it'll be in Emby in about ten minutes.
```

Nothing downloads to your disk. The library is a tree of small text files that
point at your debrid provider's URLs, so a five-thousand-item library costs a
few megabytes and appears instantly. Emby plays it like local media, complete
with artwork, resume points, and a working Skip Intro button.

## Why hand it to an agent

The unglamorous truth about a media stack is that most of the work is judgment,
not automation. Which of these forty releases is the real 1080p and not a
transcode. Is this "Extended Cut" the one you meant. Why did that anime batch
file itself as fourteen phantom movies. Rules engines are bad at this; a model
with your library in front of it is good at it.

So the policy lives in prose, in two files. `AGENTS.md` is the operating
manual — how the stack works, what to do when a metadata match fails, which
guardrails not to touch — and it is the same for everyone. `PREFS.md` is
yours: audio and subtitle policy, size ceilings, how complete a series request
is, how much the agent should ask before acting. It is gitignored, it
overrides `AGENTS.md` on any conflict, and it is the file the agent edits when
you change your mind:

- *"stop fetching anything over 20 GB, my connection is worse now"*
- *"original audio always, but never ask me about subtitle variants again"*
- *"for series, grab the whole show including specials — don't make me ask twice"*
- *"follow this show as it airs"* → it goes in the watchlist and new episodes
  arrive on their own
- *"my library looks wrong somewhere"* → `/resolve` walks the stack, finds the
  misfiled entries, and fixes the parser rather than shuffling files by hand

The agent also has the fun jobs. It can see everything you own, so *"we watched
Andor, what next"* is a real question with a real answer, and the answer can be
in your library ten minutes later without a second round trip.

## What it takes care of on its own

**Naming.** Releases arrive named like ransom notes. The sync parses show,
season, and episode out of them and files everything as
`Show Name (Year)/Season 01/…`, dropping site prefixes, group tags, quality
junk, and samples. Single-episode grabs merge into the series they belong to.
When the same episode exists twice, the better copy wins and the other is
dropped.

**Bandwidth honesty.** The whole stack shares one download cap, and releases
that could never stream inside it are refused at search time — remuxes, absurd
per-episode sizes, disc-sized movies. A library full of files that stutter is
worse than a smaller one that plays.

**Language policy.** Original audio, never a dub-only release. Dubbed
voice-overs — Latino, Castilian, Russian MVO/DVO — are filtered out rather than
discovered halfway through a film.

**Skip Intro.** It works, which is not obvious when nothing is on disk: the
stack fingerprints each episode's audio straight over the remote URL, then lets
Emby match intros per season. New episodes get their markers within a day.

**Airing shows.** A watchlist of shows to follow. It counts from the highest
episode you already have, queues only what comes after, and notices a new
season premiering instead of waiting for one you named.

## Getting started

You need Docker with `/dev/fuse`, a [Torbox](https://torbox.app) account, and
an Emby Premiere key if you want Skip Intro and hardware transcoding.

```bash
cp .env.example .env          # Torbox + Emby credentials
cp PREFS.example.md PREFS.md  # your preferences; gitignored, edit freely
docker compose up -d
python3 scripts/torbox_sync.py
python3 scripts/emby_setup.py # wizard, admin user, libraries, plugins — re-runnable
docker compose restart emby
```

Emby is on `:8096`, Prowlarr on `:9696`. Add a few indexers in Prowlarr — the
search side is empty without them — and you are done.

Then open your agent in this directory and ask it for a movie. It reads
`AGENTS.md` on its own.

## Under the hood

For the curious. You do not need any of this to use the thing.

| Piece | What it is for |
|---|---|
| `emby` | The media server: `:8096`, hardware transcoding via VAAPI |
| `torbox-mount` | rclone FUSE mount of the Torbox WebDAV tree, read-only, VFS-cached |
| `torbox-sync` | Every 15 min: turn your debrid downloads into a normalized `.strm` library, refresh Emby, probe new items |
| `prowlarr` | Indexer aggregation, so search is one query instead of six |
| `emby-throttle` | A `tc` cap on the shared bridge — one budget for every container, not one each |

The scripts under `scripts/` are the agent's hands: `torbox_find.py` searches
and ranks releases, `torbox_add.py` queues a magnet directly, `torbox_sync.py`
builds the library, `torbox_watch.py` tops up airing shows, `emby_probe.py`
forces Emby to probe streams it would otherwise ignore until playback, and
`emby_setup.py` bootstraps a fresh server. Every one of them is usable by hand
if you would rather drive.

**Things worth knowing if you rebuild this elsewhere:**

- Emby is pinned to 4.8.11. 4.9 has library-scan regressions, and the
  StrmAssistant plugin that unlocks intro detection for STRM supports 4.8.x.
- Torbox WebDAV authenticates with your account email and password, not the
  API key.
- Prowlarr's indexer list is not committed — add them by hand once.
- Never edit `library/` yourself. It is generated, and the next sync will undo
  you. Fixes belong in the sync script.
- The sync refuses to prune when the provider's API returns an incomplete
  listing. That guard exists because one timed-out request once deleted a
  thousand files. Leave it alone.

Secrets live in `.env`, chmod 600, never committed.

## License

MIT — see [LICENSE.md](LICENSE.md).
