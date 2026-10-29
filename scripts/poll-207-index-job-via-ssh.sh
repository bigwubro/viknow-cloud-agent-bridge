#!/usr/bin/env bash
# Poll 207 index job status via Tailscale SSH (no secrets in output).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JOB="${1:?job id}"
bash "$ROOT/scripts/ssh-207.sh" "JOB='${JOB}' bash -s" <<'REMOTE'
set -euo pipefail
APP_KEY=$(sudo docker inspect viknow207-platform --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^VIKNOW_APP_KEY=//p' | head -1)
for _ in $(seq 1 40); do
  body=$(curl -sf -H "X-App-Key: ${APP_KEY}" "http://127.0.0.1:5218/api/v1/knowledge/index-jobs/${JOB}")
  echo "$body" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(j.get("status"), (j.get("error_message") or "")[:120])'
  st=$(echo "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
  case "$st" in done|failed|cancelled) exit 0;; esac
  sleep 15
done
REMOTE
