#!/usr/bin/env bash
# Install nginx reverse proxy on 236 for 207 viknow-storage grant URLs (Actio GET).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
export STORAGE_PROXY_UPSTREAM="${STORAGE_PROXY_UPSTREAM:-127.0.0.1:15219}"
export VIKNOW236_STORAGE_PROXY_PORT="${VIKNOW236_STORAGE_PROXY_PORT:-5220}"

docker compose -f "$DIR/docker-compose.yml" up -d --force-recreate

echo "== smoke (expect 502/504 until 207 SSH tunnel is up on ${STORAGE_PROXY_UPSTREAM}) =="
curl -sf --connect-timeout 3 --max-time 10 "http://127.0.0.1:${VIKNOW236_STORAGE_PROXY_PORT}/health" && echo OK || echo "not ready (start deploy/207/storage-tunnel-to-236.sh on 207)"
