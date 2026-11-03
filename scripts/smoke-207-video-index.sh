#!/usr/bin/env bash
# End-to-end 207 video index smoke via Tailscale (needs VIKNOW207_SSH_PASSWORD or bastion).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/tailscale-connect-207.sh" >/dev/null
bash "$ROOT/scripts/207-storage-tunnel-via-agent.sh"
bash "$ROOT/scripts/patch-207-storage-grant-via-ssh.sh"
JOB=$(bash "$ROOT/scripts/reindex-207-video-via-ssh.sh" "${1:-207-e2e-10292328}" "${2:-vid-3ca318e6}" | awk '{print $1}')
bash "$ROOT/scripts/poll-207-index-job-via-ssh.sh" "$JOB" | tee /opt/cursor/artifacts/207-video-index-smoke.log
