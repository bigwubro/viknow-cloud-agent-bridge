#!/usr/bin/env bash
# Stop containers binding 207 ViKnow ports (best-effort).
set -euo pipefail
ports=(16432 16479 17487 17474 5198 5218 5219)
for p in "${ports[@]}"; do
  ids="$(docker ps -q --filter "publish=${p}" 2>/dev/null || true)"
  if [[ -n "$ids" ]]; then
    echo "Stopping containers publishing :${p}: ${ids}"
    docker stop $ids || true
  fi
done
for name in viknow207-ops-postgres viknow207-redis viknow207-neo4j; do
  docker rm -f "$name" 2>/dev/null || true
done
