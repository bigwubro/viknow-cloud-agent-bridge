#!/usr/bin/env bash
# Patch running actio-server-8600 on 236 to rewrite 207 grant URLs → http://172.21.0.1:5220
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${ACTIO_CONTAINER:-actio-server-8600}"
TARGET="/app/services/video_split/shared/media_input.py"

docker cp "$ROOT/scripts/patch_actio_207_grant_proxy.py" "$CONTAINER:/tmp/patch_actio_207_grant_proxy.py"
docker exec "$CONTAINER" python3 /tmp/patch_actio_207_grant_proxy.py "$TARGET"
docker restart "$CONTAINER"
sleep 3
curl -sf --connect-timeout 5 http://127.0.0.1:8600/health | head -c 120
echo
echo "actio grant proxy patch applied"
