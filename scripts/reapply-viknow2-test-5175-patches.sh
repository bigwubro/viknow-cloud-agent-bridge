#!/usr/bin/env bash
# Re-apply all 5175 hotfixes to viknow2-test (idempotent). Run on 236 host.
set -euo pipefail
CONTAINER="${VIKNOW2_TEST_CONTAINER:-viknow2-test}"
DIR="$(cd "$(dirname "$0")" && pwd)"
for f in patch5175_v4.py patch5175_v6.py patch5175_v7.py patch5175_v5.py patch5175_v3.py; do
  [ -f "$DIR/$f" ] || continue
  docker cp "$DIR/$f" "$CONTAINER:/tmp/$f"
  docker exec "$CONTAINER" python3 "/tmp/$f" || true
done
docker cp "$DIR/patch5175.py" "$CONTAINER:/tmp/patch5175.py"
docker exec "$CONTAINER" python3 /tmp/patch5175.py || true
docker restart "$CONTAINER"
echo "Re-applied 5175 patches and restarted $CONTAINER"
