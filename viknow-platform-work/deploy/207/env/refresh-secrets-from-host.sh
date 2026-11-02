#!/usr/bin/env bash
# Merge host deploy.env + pg-demo into deploy/207/env/secrets.env (207 layout).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
HOST_ROOT="${VIKNOW236_HOST_ROOT:-${HOME}}"

merge() {
  local out="$1"; shift
  local f
  : >"$out"
  for f in "$@"; do
    [[ -f "$f" ]] || continue
    grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$f" || true
  done | awk -F= '!seen[$1]++' >>"$out"
}

mkdir -p deploy/207/env
merge deploy/207/env/secrets.env \
  deploy/207/env/secrets.env \
  "$HOST_ROOT/viknow-platform/pg-demo.env" \
  "$HOST_ROOT/deploy.env" \
  "$HOST_ROOT/.viknow-platform/ops.env"

for key in \
  VIKNOW_HOST VIKNOW_PORT VIKNOW_UI_HOST VIKNOW_UI_PORT VIKNOW_PLAYGROUND_API_URL \
  VIKNOW_CORS_ORIGINS VIKNOW_STORAGE_URL \
  VIKNOW_DATA_DIR VIKNOW_LIBRARIES_ROOT VIKNOW_SCRATCH_DIR VIKNOW_AGENT_WORKSPACE_ROOT \
  VIKNOW_STATE_DIR VIKNOW_VENDOR_FILES_ROOT HYPERRAG_TEST_IMAGE ACTIO_SERVER_URL \
  VIKNOW_FEISHU_ENABLED FEISHU_APP_ID FEISHU_APP_SECRET; do
  sed -i "/^${key}=/d" deploy/207/env/secrets.env 2>/dev/null || sed -i '' "/^${key}=/d" deploy/207/env/secrets.env
done

grep -v -E '^(JUICEFS_|MINIO_|POSTGRES_|REDIS_|NEO4J_)' deploy/207/env/secrets.env >deploy/207/env/secrets.env.tmp
mv deploy/207/env/secrets.env.tmp deploy/207/env/secrets.env

# Force Actio on 236 when host deploy.env still says 127.0.0.1
sed -i 's|^ACTIO_SERVER_URL=.*|ACTIO_SERVER_URL=http://36.103.198.236:8600|' deploy/207/env/secrets.env 2>/dev/null || \
  sed -i '' 's|^ACTIO_SERVER_URL=.*|ACTIO_SERVER_URL=http://36.103.198.236:8600|' deploy/207/env/secrets.env

# Ensure storage token + app key from host.env if present
if [[ -n "${STORAGE_TOKEN:-}" ]]; then
  grep -q '^VIKNOW_STORAGE_TOKEN=' deploy/207/env/secrets.env || echo "VIKNOW_STORAGE_TOKEN=${STORAGE_TOKEN}" >>deploy/207/env/secrets.env
fi
if [[ -n "${VIKNOW_APP_KEY:-}" ]]; then
  grep -q '^VIKNOW_APP_KEY=' deploy/207/env/secrets.env || echo "VIKNOW_APP_KEY=${VIKNOW_APP_KEY}" >>deploy/207/env/secrets.env
fi

echo "Wrote deploy/207/env/secrets.env ($(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' deploy/207/env/secrets.env || echo 0) keys)"
