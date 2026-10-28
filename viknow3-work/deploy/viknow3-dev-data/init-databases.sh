#!/usr/bin/env bash
# Create ViKnow 3 databases on viknow3-ops-postgres (127.0.0.1:16432).
set -euo pipefail

CONTAINER="${VIKNOW3_PG_CONTAINER:-viknow3-ops-postgres}"
USER="${VIKNOW3_PG_USER:-viknow_ops}"

databases=(
  viknow3_access
  viknow3_access_test
  viknow3_agent
  viknow3_agent_test
  viknow3_ops
  viknow3_ops_test
  viknow3_knowledge
  viknow3_knowledge_test
)

for db in "${databases[@]}"; do
  exists="$(docker exec "$CONTAINER" psql -U "$USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${db}'" || true)"
  if [[ "$exists" != "1" ]]; then
    echo "CREATE DATABASE ${db} OWNER ${USER}"
    docker exec "$CONTAINER" psql -U "$USER" -d postgres -c \
      "CREATE DATABASE \"${db}\" OWNER \"${USER}\";"
  else
    echo "skip existing ${db}"
  fi
done

echo "done"
