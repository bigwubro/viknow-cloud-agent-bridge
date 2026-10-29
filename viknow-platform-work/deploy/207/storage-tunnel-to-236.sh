#!/usr/bin/env bash
# On 207: keep SSH reverse tunnel so 236 nginx (127.0.0.1:15219) reaches local viknow-storage :5219.
# Requires 207 host SSH key in cursor-agent@36.103.198.236 authorized_keys (BatchMode).
set -euo pipefail

VIKNOW236_TUNNEL_HOST="${VIKNOW236_TUNNEL_HOST:-36.103.198.236}"
VIKNOW236_TUNNEL_USER="${VIKNOW236_TUNNEL_USER:-cursor-agent}"
VIKNOW207_TUNNEL_REMOTE_PORT="${VIKNOW207_TUNNEL_REMOTE_PORT:-15219}"
VIKNOW_STORAGE_HTTP_PORT="${VIKNOW_STORAGE_HTTP_PORT:-5219}"

SSH_OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=yes
)

REMOTE="127.0.0.1:${VIKNOW207_TUNNEL_REMOTE_PORT}:127.0.0.1:${VIKNOW_STORAGE_HTTP_PORT}"

if command -v autossh >/dev/null 2>&1; then
  exec autossh -M 0 -N "${SSH_OPTS[@]}" \
    -R "$REMOTE" \
    "${VIKNOW236_TUNNEL_USER}@${VIKNOW236_TUNNEL_HOST}"
fi

echo "autossh not found; using ssh -N (reconnect manually on failure)" >&2
while true; do
  ssh -N "${SSH_OPTS[@]}" -R "$REMOTE" "${VIKNOW236_TUNNEL_USER}@${VIKNOW236_TUNNEL_HOST}" || true
  sleep 5
done
