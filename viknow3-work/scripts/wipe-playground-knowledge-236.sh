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
if docker ps --format '{{.Names}}' | grep -qx viknow3-ops-postgres; then
  docker exec viknow3-ops-postgres psql -U viknow_ops -d viknow3_knowledge -v ON_ERROR_STOP=1 -c \
    "DELETE FROM hyperrag.hyperrag_bm25_chunks;
     DELETE FROM hyperrag.hyperrag_asset_status;
     DELETE FROM hyperrag.hyperrag_knowledge_discovery_assets;
     DELETE FROM hyperrag.hyperrag_knowledge_discovery_libraries;
     DELETE FROM hyperrag.hyperrag_knowledgebase_info;
     DELETE FROM catalog.knowledge_index_jobs;
     DELETE FROM catalog.knowledge_assets;
     DELETE FROM catalog.knowledge_libraries;"
else
  DSN="${VIKNOW_KNOWLEDGE_DSN:-${VIKNOW_HYPERRAG_DSN:-}}"
  if [[ -z "$DSN" ]]; then
    echo "skip PG wipe: no viknow3-ops-postgres and no DSN"
    exit 0
  fi
  psql "$DSN" -v ON_ERROR_STOP=1 -c \
    "DELETE FROM catalog.knowledge_index_jobs; DELETE FROM catalog.knowledge_assets; DELETE FROM catalog.knowledge_libraries;"
fi
echo "done"
