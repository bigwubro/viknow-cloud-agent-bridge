#!/usr/bin/env bash
# DEPRECATED (product): anonymous public/ prefix was removed from app docs (Feishu §1.6 presigned TTL).
# Configure anonymous read (download only) for objects under viknow/public/ on JuiceFS S3 gateway.
# Requires: docker, gateway credentials in env (GATEWAY_ACCESS_KEY, GATEWAY_SECRET_KEY).
# Run after gateway deploy or recreate (policy is stored in gateway metadata; re-apply if lost).
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:19191}"
BUCKET="${VIKNOW_S3_BUCKET:-viknow}"
PUBLIC_PREFIX="${VIKNOW_S3_PUBLIC_PREFIX:-public/}"

: "${GATEWAY_ACCESS_KEY:?GATEWAY_ACCESS_KEY is required}"
: "${GATEWAY_SECRET_KEY:?GATEWAY_SECRET_KEY is required}"

docker run --rm --network host --entrypoint /bin/sh minio/mc:latest -c "
  mc alias set vgw '${GATEWAY_URL}' '${GATEWAY_ACCESS_KEY}' '${GATEWAY_SECRET_KEY}' &&
  mc anonymous set download vgw/${BUCKET}/${PUBLIC_PREFIX} &&
  mc anonymous get vgw/${BUCKET}/${PUBLIC_PREFIX}
"

echo "Anonymous download enabled for ${BUCKET}/${PUBLIC_PREFIX}"
echo "Example URL (no signature): ${GATEWAY_URL}/${BUCKET}/${PUBLIC_PREFIX}<path>/<file>"
