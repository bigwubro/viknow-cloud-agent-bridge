#!/usr/bin/env bash
# Keep one SSH control master to 236 so later `ssh 236 '...'` reuse the socket (no full reconnect).
set -euo pipefail
HOST="${VIKNOW236_SSH_HOST:-236}"
CONTROL="${HOME}/.ssh/cm-cursor-agent@36.103.198.236:22"
if ssh -O check "$HOST" 2>/dev/null; then
  echo "SSH master to 236 already up"
  exit 0
fi
mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
ssh -fN "$HOST"
sleep 1
if ssh -O check "$HOST" 2>/dev/null; then
  echo "SSH master to 236 started (ControlPath ${CONTROL})"
else
  echo "failed to start SSH master to 236" >&2
  exit 1
fi
