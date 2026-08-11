---
name: resolve
description: Diagnose Torbox sync and Emby health, then find and fix unrecognized/misfiled library entries. Use when asked to check the stack's health, "is emby still classifying stuff", or to clean up unrecognized movies/shows.
---

# /resolve — diagnose the stack, then fix misfiled library entries

Two phases. Do not skip phase 1 — most "unrecognized item" reports turn out to
be a live sync problem, not a stale metadata problem.

## Phase 1 — verify Torbox sync and Emby health

1. `docker compose ps` — all four services should be `Up`. Note Emby's
   container uptime vs its actual process uptime; they can differ.
2. `docker compose logs torbox-sync --tail 300` — look for the most recent
   `strm sync: N wanted, ... created, ... removed` line (confirms the sync
   itself completed) and for `[skip-prune]` (confirms a source failed to list
   and pruning was correctly refused — see AGENTS.md). A `Connection refused`
   traceback around `emby_probe.py` during the sync loop usually means Emby's
   *internal* process was mid-restart at that moment (grep
   `docker compose logs emby --tail 400 | grep -i restart` for
   `restartexitcode 3` / `AutomaticRestartEntryPoint` to confirm) — it is not
   itself an ongoing problem if Emby responds now.
3. Confirm Emby is live: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8096/emby/System/Ping`
   should return `200`.
4. Check for an active/stuck scan:
   `curl -s "http://localhost:8096/emby/ScheduledTasks?api_key=$(cat sync-state/emby_api_key)"`
   — look at the `RefreshLibrary` task's `State` (`Idle` vs `Running`).
5. Trigger a fresh pass before inspecting content:
   `python3 scripts/torbox_sync.py && curl -s -X POST "http://localhost:8096/emby/Library/Refresh?api_key=$(cat sync-state/emby_api_key)" && python3 scripts/emby_probe.py --limit 60`

Only move to phase 2 once sync is confirmed current and Emby is responsive.

## Phase 2 — find and fix unrecognized items

### Pull the listing

`curl`'s own output gets truncated by this environment's `rtk` tee wrapper on
large responses — a `>` redirect captures only the truncated preview, not the
full body. If the tool output ends with `[full output: ~/.local/share/rtk/tee/*.log]`,
`cp` that log file instead of trusting the redirect target:

```bash
curl -s "http://localhost:8096/emby/Items?api_key=$(cat sync-state/emby_api_key)&Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds,ProductionYear,Path&Limit=5000" > /tmp/items.json
# if truncated:
cp "$(ls -t ~/.local/share/rtk/tee/*.log | head -1)" /tmp/items.json
```

Emby's raw JSON sometimes contains control characters that break `python3 -m
json.tool` / plain `json.load`; `jq` handles it fine — prefer `jq` for this file.

### Find the unrecognized ones

An item with no Tmdb/Imdb/Tvdb id is the reliable "Emby couldn't identify
this" signal — more reliable than eyeballing names:

```bash
jq -r '.Items[] | select((.ProviderIds == null) or
  ((.ProviderIds.Tmdb == null) and (.ProviderIds.Imdb == null) and (.ProviderIds.Tvdb == null)))
  | "\(.Type)\t\(.Name)\t\(.Path)"' /tmp/items.json
```

### Diagnose each one — don't guess, read the source

For every stray item, `cat` its `.strm` file — the URL's `torrent_id=`/`usenet_id=`/`web_id=`
query param tells you exactly which Torbox download produced it:

```bash
grep -oP '(torrent|usenet|web)_id=\d+' "<path>.strm"
```

Then pull that item's real file listing from Torbox (not just the indexer's
one-line description) to see the actual internal filenames:

```bash
python3 -c "
import sys, json
sys.path.insert(0, 'scripts')
from torbox_add import load_env
import urllib.request
key = load_env()['TORBOX_API_KEY']
req = urllib.request.Request('https://api.torbox.app/v1/api/torrents/mylist?bypass_cache=true',
    headers={'Authorization': f'Bearer {key}', 'User-Agent': 'debrid-emby-stack/1.0'})
data = json.load(urllib.request.urlopen(req, timeout=60))['data']
item = next(i for i in data if i['id'] == YOUR_ID)
print(item['name'], item.get('download_state'))
for f in item['files']: print(' ', f['name'])
"
```

Test the actual parser against the actual filename before changing anything:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
import torbox_sync as t
print(t.tv_target('<item name>', '<filename>'))
"
```

### Fix hierarchy (cheapest/safest first)

1. **Movie with a mangled title, still one identifiable film** — add a
   `TITLE_OVERRIDES` entry in `scripts/torbox_sync.py` keyed by the exact
   lowercased `clean_show()` output (compute it: `t.clean_show(stem)`).
2. **Same show landing under multiple differently-spelled top-level
   folders** (apostrophe variants, romaji vs English title, a release that
   embeds "Part N" instead of the franchise name) — add `ALIASES` entries.
   **The merge strips a trailing `(YYYY)` before comparing** — alias to the
   *stripped* (no-year) lowercased form, not the year-suffixed form, or the
   entries silently land in a different group than the real canonical folder
   and don't merge. Verify before syncing:
   ```bash
   python3 -c "
   import sys, re; sys.path.insert(0, 'scripts'); import torbox_sync as t
   for top in ['<variant 1>', '<variant 2>', '<canonical>']:
       stripped = re.sub(r'\s*\((?:19|20)\d{2}(?:-(?:19|20)\d{2})?\)\$', '', top).lower()
       print(top, '->', t.ALIASES.get(top.lower()) or t.ALIASES.get(stripped) or stripped)
   "
   ```
   All variants meant to merge must print the *same* final string.
3. **A whole release's episodes are landing as phantom movies (or in the
   wrong season)** because its filenames don't match `tv_target()`'s regexes
   (`EP_TOKEN`/`X_TOKEN`/`DOT_EP_PAT`/`ANIME_EP`/`ANIME_EP_DOT`) — before
   touching the regexes, check whether a **differently-named release of the
   same content** already uses parser-friendly `SxxExx` naming (an NF/AMZN
   WEB-DL scene release almost always does; a bare fansub batch numbered
   `NN - Title.mkv` or `Title - NN.mkv` with no season token often doesn't).
   Swapping the source is far lower-risk than widening a regex blind. Verify
   the replacement's internal naming via the mylist API call above *before*
   deleting the original — confirm it's `cached` and its filenames actually
   contain `SxxExx`.
4. **A genuine regex gap with no reasonable substitute release** — only then
   patch the regex, and only additively (widen an anchor, add a leading `\b`)
   — never loosen a pattern in a way that could swallow unrelated content.
   Test the fix against the failing filename AND against the pattern's
   existing documented cases (the comment above each regex names them) before
   applying it, so you're not fixing one show by breaking another.
5. **Absolute-numbered release with no franchise-wide season marker at all**
   (e.g. a part/cour of a multi-season franchise released as "episode 1-N"
   with no season token anywhere) — merging it into a unified multi-season
   show via `ALIASES` will default it to Season 1 and can silently collide
   with (and delete, via the quality dedupe) a different season's real
   episodes sharing the same episode numbers. Prefer a release that already
   carries the correct season number in its own filenames over inferring one.

### Never hand-edit `library/`

Every fix above changes `scripts/torbox_sync.py` or swaps which Torbox
source backs a `.strm` file — never rename/move/delete files under `library/`
directly (see AGENTS.md). After each fix:

```bash
python3 scripts/torbox_sync.py && curl -s -X POST "http://localhost:8096/emby/Library/Refresh?api_key=$(cat sync-state/emby_api_key)" && python3 scripts/emby_probe.py --limit 60
```

Then re-verify the specific folder(s) you touched on disk (`find .../library/tv/<show> -name '*.strm' | wc -l`
and check the season boundaries look right) before moving to the next item —
a merge that goes wrong (wrong season, name collision) is easy to cause and
easy to miss if you only check that *a* folder now exists.

### Deleting superseded Torbox sources

When a fix means an old source is now fully redundant (swapped for a
better-named or better-quality release), delete it — Torbox deletes are
irreversible and the policy is delete-then-tell, not ask-first (AGENTS.md).
Use the `User-Agent` header or the API 403s with Cloudflare error 1010:

```bash
python3 -c "
import sys, json
sys.path.insert(0, 'scripts')
from torbox_add import load_env
import urllib.request
key = load_env()['TORBOX_API_KEY']
req = urllib.request.Request('https://api.torbox.app/v1/api/torrents/controltorrent',
    data=json.dumps({'torrent_id': ID, 'operation': 'delete'}).encode(),
    headers={'Authorization': f'Bearer {key}', 'User-Agent': 'debrid-emby-stack/1.0', 'Content-Type': 'application/json'},
    method='POST')
print(urllib.request.urlopen(req, timeout=60).read().decode())
"
```

### What not to chase

Report rather than force a fix when:
- The item lives outside `library/tv` or `library/movies` (e.g. `/media/anime`) —
  that's not `torbox_sync.py`'s domain.
- The fix would need restructuring the fallback (non-`tv_target()`) classification
  path to inject proper `Season 00` folder semantics for a handful of bonus
  episodes — low value, meaningfully more risk than the alias/override fixes above.
- You can't tell what a garbage-named file actually is without guessing.

List these clearly at the end rather than silently dropping them.
