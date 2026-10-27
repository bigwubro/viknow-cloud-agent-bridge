#!/usr/bin/env bash
# Wipe Playground knowledge catalog + viknow-storage libraries on 236 (dev only).
set -euo pipefail

source /home/cursor-agent/viknow3/pg-demo.env
TOKEN="${VIKNOW_STORAGE_TOKEN:?}"
BASE="${VIKNOW_STORAGE_URL:-http://127.0.0.1:8090}"

echo "=== viknow-storage libraries ==="
libs=$(curl -sf -H "Authorization: Bearer $TOKEN" "$BASE/libraries?limit=500" | python3 -c "import json,sys; print(' '.join(r['library_id'] for r in json.load(sys.stdin)))" || true)
for lib in $libs; do
  echo "delete storage library $lib"
  curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE/libraries/$lib" || true
done

echo "=== viknow knowledge catalog (playground DB) ==="
DSN="${VIKNOW_KNOWLEDGE_DSN:-${VIKNOW_HYPERRAG_DSN:-}}"
if [[ -z "$DSN" ]]; then
  echo "skip PG wipe: set VIKNOW_KNOWLEDGE_DSN or VIKNOW_HYPERRAG_DSN"
  exit 0
fi
psql "$DSN" -v ON_ERROR_STOP=1 <<'SQL'
DELETE FROM catalog.knowledge_index_jobs;
DELETE FROM catalog.knowledge_assets;
DELETE FROM catalog.knowledge_libraries;
SQL
echo "done"
