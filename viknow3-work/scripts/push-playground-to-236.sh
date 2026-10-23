#!/usr/bin/env bash
# From dev machine: rsync only UI + knowledge slice to 236, then restart (seconds, not minutes).
set -euo pipefail

HOST="${VIKNOW_236_HOST:-cursor-agent@36.103.198.236}"
KEY="${VIKNOW_236_SSH_KEY_FILE:-$HOME/.ssh/id_ed25519}"
REMOTE_ROOT="${VIKNOW_236_VIKNOW3:-/home/cursor-agent/src/viknow3}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$HOST")
RSYNC=(rsync -az --delete -e "ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new")

PATHS=(
  ui/playground_routes.py
  ui/server.py
  ui/static/playground/
  src/viknow3/access/knowledge/
  src/viknow3/storage/
  src/viknow3/server.py
  scripts/deploy-playground-ui.sh
)

for rel in "${PATHS[@]}"; do
  src="${LOCAL_ROOT}/${rel}"
  dest="${REMOTE_ROOT}/${rel}"
  if [[ -d "$src" ]]; then
    "${RSYNC[@]}" "$src" "${HOST}:${dest%/}/"
  else
    "${RSYNC[@]}" "$src" "${HOST}:$(dirname "$dest")/"
  fi
done

"${SSH[@]}" "chmod +x ${REMOTE_ROOT}/scripts/deploy-playground-ui.sh && ${REMOTE_ROOT}/scripts/deploy-playground-ui.sh restart"
