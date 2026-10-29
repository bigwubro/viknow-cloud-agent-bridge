#!/usr/bin/env bash
set -euo pipefail
CONTAINER="${VIKNOW_OPS_PG_CONTAINER:-viknow207-ops-postgres}"
USER="${VIKNOW_OPS_PG_USER:-viknow_ops}"

for db in viknow_access viknow_agent viknow_knowledge viknow_storage; do
  exists="$(docker exec "$CONTAINER" psql -U "$USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" 2>/dev/null || true)"
  if [[ "$exists" == "1" ]]; then
    echo "database ${db} already exists"
  else
    docker exec "$CONTAINER" psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE \"${db}\" OWNER \"${USER}\";"
    echo "created database ${db}"
  fi
done
