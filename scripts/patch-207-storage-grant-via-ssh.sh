#!/usr/bin/env bash
# Update 207 storage grant base + platform fetch base; recreate storage container (Tailscale SSH).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GRANT_BASE="${VIKNOW_STORAGE_GRANT_FETCH_BASE:-http://172.21.0.1:5220}"
bash "$ROOT/scripts/ssh-207.sh" "GRANT_BASE='${GRANT_BASE}' bash -s" <<'REMOTE'
set -euo pipefail
HOST_ENV=~/viknow207.host.env
PLATFORM_ENV=~/viknow207.platform.env
for f in "$HOST_ENV" "$PLATFORM_ENV"; do
  touch "$f"
  if grep -q '^STORAGE_DOWNLOAD_BASE_URL=' "$f"; then
    sed -i "s|^STORAGE_DOWNLOAD_BASE_URL=.*|STORAGE_DOWNLOAD_BASE_URL=${GRANT_BASE}|" "$f"
  else
    echo "STORAGE_DOWNLOAD_BASE_URL=${GRANT_BASE}" >>"$f"
  fi
  if grep -q '^VIKNOW_STORAGE_GRANT_FETCH_BASE=' "$f"; then
    sed -i "s|^VIKNOW_STORAGE_GRANT_FETCH_BASE=.*|VIKNOW_STORAGE_GRANT_FETCH_BASE=${GRANT_BASE}|" "$f"
  else
    echo "VIKNOW_STORAGE_GRANT_FETCH_BASE=${GRANT_BASE}" >>"$f"
  fi
done
for dir in ~/src/viknow-platform ~/viknow-platform; do
  if [[ -f "$dir/deploy/207/docker-compose.stack.yml" ]]; then
    COMPOSE_ROOT="$dir"
    break
  fi
done
if [[ -n "${COMPOSE_ROOT:-}" && -f "$HOST_ENV" ]]; then
  cd "$COMPOSE_ROOT"
  set -a && source "$HOST_ENV" && set +a
  sudo docker compose -f deploy/207/docker-compose.stack.yml --env-file "$HOST_ENV" up -d --force-recreate storage
fi
curl -sf http://127.0.0.1:5219/health >/dev/null
echo patched_207_storage
REMOTE
