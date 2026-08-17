#!/usr/bin/env bash
# gamemode custom hook: wind the discovery/indexing services down while a
# game runs, bring them back when it exits. Emby and torbox-mount stay up
# so a movie can keep streaming. Wired in ~/.config/gamemode.ini [custom].
#
# Idempotent: `docker compose stop/start` on an already stopped/started
# container is a no-op, so a missed or doubled event leaves no mess.
set -u
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

PROJECT=/mnt/archive4/DEBRID
LOG="$PROJECT/sync-state/gamemode-hook.log"
STAMP="$(date '+%F %T')"

compose() { docker compose --project-directory "$PROJECT" "$@"; }

case "${1:-}" in
  start)
    # One call stops them in parallel; --timeout 5 so gamemode is not kept
    # waiting on a slow container while the game sits on its launcher.
    compose stop --timeout 5 \
      flaresolverr prowlarr torbox-sync cueslice emby-throttle
    echo "$STAMP start: discovery stack stopped" >> "$LOG"
    ;;
  end)
    # torbox-mount never went down, so no mount work here; torbox-sync
    # retries Prowlarr in its loop, so start order does not matter.
    compose start prowlarr flaresolverr torbox-sync cueslice
    echo "$STAMP end: discovery stack started" >> "$LOG"
    ;;
  *)
    echo "usage: $0 start|end" >&2
    exit 2
    ;;
esac
