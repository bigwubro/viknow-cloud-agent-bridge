#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
HOST_ROOT="${VIKNOW236_HOST_ROOT:-/home/cursor-agent}"
merge() {
  local out="$1"; shift
  local f
  : >"$out"
  for f in "$@"; do
    [[ -f "$f" ]] || continue
    grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$f" || true
  done | awk -F= '!seen[$1]++' >>"$out"
}
merge deploy/236/env/secrets.env \
  "$HOST_ROOT/viknow-platform/pg-demo.env" \
  "$HOST_ROOT/viknow-platform/demo.env" \
  "$HOST_ROOT/deploy.env" \
  "$HOST_ROOT/.viknow-platform/ops.env" \
  "$HOST_ROOT/.viknow-platform/deepagents-langfuse.env"

# --- Keys that belong in api.env / ui.env / Dockerfile only (never secrets) ---
for key in \
  VIKNOW_HOST VIKNOW_PORT VIKNOW_UI_HOST VIKNOW_UI_PORT VIKNOW_PLAYGROUND_API_URL \
  VIKNOW_CORS_ORIGINS VIKNOW_PLAYGROUND_CORS_ORIGINS \
  VIKNOW_STORAGE_URL VIKNOW_S3_PUBLIC_ENDPOINT \
  VIKNOW_DATA_DIR VIKNOW_LIBRARIES_ROOT VIKNOW_SCRATCH_DIR VIKNOW_AGENT_WORKSPACE_ROOT \
  VIKNOW_STATE_DIR VIKNOW_VENDOR_FILES_ROOT HYPERRAG_TEST_IMAGE ACTIO_SERVER_URL \
  VIKNOW_STORAGE_PUBLIC_HOST VIKNOW_STORAGE_PUBLIC_PORT VIKNOW_STORAGE_PUBLIC_URL VIKNOW_STORAGE_BROWSER_URL \
  VIKNOW_E2E_MINERU_WRAPPER PYTHONPATH VIKNOW_PLATFORM_ROOT VIKNOW_HYPERRAG_REPO_ROOT VIKNOW_MINERU_BIN_DIR \
  VIKNOW_FEISHU_ENABLED FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_SPACE_ID FEISHU_TENANT_HOST; do
  sed -i "/^${key}=/d" deploy/236/env/secrets.env
done

# --- deploy.env / viknow2 stack noise: viknow-platform reads DSN + ops table, not duplicate infra hosts ---
for key in \
  VIKNOW_FILES_ROOT VIKNOW_OPS_TEST_DSN VIKNOW_SDK_JS \
  VIKNOW_ADMIN_PASSWORD VIKNOW_ASR_MODEL VIKNOW_PERSISTENCE_BACKEND \
  VIKNOW_PROMETHEUS_BASE_URL VIKNOW_VLLM_MONITOR_BASE_URL VIKNOW_LOCATE_ANYTHING_BASE_URL \
  VIKNOW1_API VIKNOW1_TOKEN; do
  sed -i "/^${key}=/d" deploy/236/env/secrets.env
done

# Prefixes: JuiceFS / MinIO / raw PG-Redis-Neo4j host vars (connections live in viknow_ops, not env)
grep -v -E '^(JUICEFS_|MINIO_|POSTGRES_|REDIS_|NEO4J_)' deploy/236/env/secrets.env >deploy/236/env/secrets.env.tmp
mv deploy/236/env/secrets.env.tmp deploy/236/env/secrets.env

sed -i 's|host.docker.internal|127.0.0.1|g' deploy/236/env/secrets.env
sed -i "s|:8871|:8600|g" deploy/236/env/secrets.env
echo "Wrote deploy/236/env/secrets.env ($(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' deploy/236/env/secrets.env || echo 0) keys)"
