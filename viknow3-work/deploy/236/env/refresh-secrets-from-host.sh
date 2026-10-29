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
  "$HOST_ROOT/viknow3/pg-demo.env" \
  "$HOST_ROOT/viknow3/demo.env" \
  "$HOST_ROOT/deploy.env" \
  "$HOST_ROOT/.viknow3/ops.env" \
  "$HOST_ROOT/.viknow3/deepagents-langfuse.env"

# Non-secret keys belong in api.env / ui.env only
for key in \
  VIKNOW_HOST VIKNOW_PORT VIKNOW_UI_HOST VIKNOW_UI_PORT VIKNOW_PLAYGROUND_API_URL \
  VIKNOW_CORS_ORIGINS VIKNOW_STORAGE_URL VIKNOW_S3_PUBLIC_ENDPOINT \
  VIKNOW3_DATA_DIR VIKNOW3_LIBRARIES_ROOT VIKNOW_SCRATCH_DIR VIKNOW_AGENT_WORKSPACE_ROOT \
  VIKNOW_STATE_DIR VIKNOW_VENDOR_FILES_ROOT HYPERRAG_TEST_IMAGE ACTIO_SERVER_URL; do
  sed -i "/^${key}=/d" deploy/236/env/secrets.env
done

sed -i 's|host.docker.internal|127.0.0.1|g' deploy/236/env/secrets.env
sed -i "s|:8871|:8600|g" deploy/236/env/secrets.env
sed -i "s|ACTIO_SERVER_URL=.*|ACTIO_SERVER_URL=http://127.0.0.1:8600|" deploy/236/env/secrets.env
echo "Wrote deploy/236/env/secrets.env ($(wc -l < deploy/236/env/secrets.env) lines)"
