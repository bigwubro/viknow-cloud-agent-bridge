#!/usr/bin/env bash
# Re-trigger video index on 207 library (Tailscale). Requires 207 platform env in container.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="${1:-207-e2e-10292328}"
ASSET="${2:-vid-3ca318e6}"
bash "$ROOT/scripts/ssh-207.sh" "LIB='${LIB}' ASSET='${ASSET}' bash -s" <<'REMOTE'
set -euo pipefail
APP_KEY=$(sudo docker inspect viknow207-platform --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^VIKNOW_APP_KEY=//p' | head -1)
if [[ -z "$APP_KEY" ]]; then echo missing_app_key; exit 1; fi
job=$(curl -sf -X POST "http://127.0.0.1:5218/api/v1/knowledge/index-jobs" \
  -H "X-App-Key: ${APP_KEY}" -H "Content-Type: application/json" \
  -d "{\"library_id\":\"${LIB}\",\"asset_id\":\"${ASSET}\",\"params\":{\"title\":\"video-retest\"}}")
echo "$job" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(j.get("job_id"), j.get("status"))'
REMOTE
