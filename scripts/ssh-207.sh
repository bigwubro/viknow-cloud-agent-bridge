#!/usr/bin/env bash
# SSH to 207 (QX-AI-07) over Tailscale. Requires: bash scripts/tailscale-connect-207.sh first.
set -euo pipefail
TARGET="${VIKNOW207_SSH_TARGET:-wuzihan@100.101.46.52}"
PASS="${VIKNOW207_SSH_PASSWORD:-${VIKNOW_BASTION_SSH_PASSWORD:-}}"
if [[ -z "$PASS" ]]; then
  echo "set VIKNOW207_SSH_PASSWORD or VIKNOW_BASTION_SSH_PASSWORD" >&2
  exit 1
fi
command -v sshpass >/dev/null || { echo "install sshpass" >&2; exit 1; }
export SSHPASS="$PASS"
exec sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$TARGET" "$@"
