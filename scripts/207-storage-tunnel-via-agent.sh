#!/usr/bin/env bash
# Cloud Agent: Tailscale to 207 + SSH reverse tunnel 236:15219 → 207:5219 for nginx proxy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="${VIKNOW207_TUNNEL_SESSION:-207-storage-tunnel-236}"
TS_HOST="${VIKNOW207_TAILSCALE_HOST:-100.101.46.52}"
REMOTE_PORT="${VIKNOW207_TUNNEL_REMOTE_PORT:-15219}"
STORAGE_PORT="${VIKNOW207_STORAGE_PORT:-5219}"

bash "$ROOT/scripts/tailscale-connect-207.sh" >/dev/null

TMUX=(tmux -f /exec-daemon/tmux.portal.conf)
if "${TMUX[@]}" has-session -t "=$SESSION" 2>/dev/null; then
  echo "tunnel session $SESSION already running"
  exit 0
fi

"${TMUX[@]}" new-session -d -s "$SESSION" -c "$ROOT" -- "${SHELL:-bash}" -l
CMD="ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -N -R 127.0.0.1:${REMOTE_PORT}:${TS_HOST}:${STORAGE_PORT} cursor-agent@36.103.198.236"
"${TMUX[@]}" send-keys -t "$SESSION:0.0" "$CMD" C-m
sleep 3
ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes cursor-agent@36.103.198.236 \
  "curl -sf --connect-timeout 8 http://127.0.0.1:${REMOTE_PORT}/health && curl -sf --connect-timeout 8 http://127.0.0.1:5220/health"
echo "OK: agent tunnel + 236 proxy"
