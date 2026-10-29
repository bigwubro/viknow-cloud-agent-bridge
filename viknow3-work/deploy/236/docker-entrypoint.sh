#!/usr/bin/env bash
set -euo pipefail
cd "${VIKNOW3_ROOT:-/opt/viknow3}"

export PATH="/opt/viknow3/bin:${PATH}"
export PYTHONPATH="${PYTHONPATH:-/opt/viknow3/src}"
export VIKNOW_HYPERRAG_REPO_ROOT="${VIKNOW_HYPERRAG_REPO_ROOT:-/opt/viknow3/external/HyperRAG}"

if [[ -n "${ACTIO_SERVER_URL:-}" ]]; then
  export ACTIO_SERVER_URL="${ACTIO_SERVER_URL//host.docker.internal/127.0.0.1}"
fi
if [[ "${ACTIO_SERVER_URL:-}" == *":8871"* ]]; then
  export ACTIO_SERVER_URL="${ACTIO_SERVER_URL//:8871/:8600}"
fi

if [[ "${VIKNOW_PRIVATE_BASE_URL:-}" == http://*":8443"* ]]; then
  export VIKNOW_PRIVATE_BASE_URL="https://${VIKNOW_PRIVATE_BASE_URL#http://}"
fi

mkdir -p "${VIKNOW_SCRATCH_DIR:-/tmp/scratch}" \
  "${VIKNOW_AGENT_WORKSPACE_ROOT:-/tmp/agent-workspaces}" \
  "${VIKNOW3_LIBRARIES_ROOT:-/data/viknow/libraries}" \
  "${VIKNOW_STATE_DIR:-/state}"
export JIEBA_CACHE_DIR="${JIEBA_CACHE_DIR:-${VIKNOW_SCRATCH_DIR}/jieba-cache}"
export TMPDIR="${TMPDIR:-${VIKNOW_SCRATCH_DIR}/tmp}"
mkdir -p "$JIEBA_CACHE_DIR" "$TMPDIR"

export_mineru_vlm_env() {
  eval "$(python3 - <<'PY'
import os
import shlex
from viknow3.ops.config.model_resolve import resolve_openai_chat
from viknow3.ops.config.runtime import load_runtime_bundle
private = load_runtime_bundle().providers.get("private") or {}
resolved = resolve_openai_chat("server-default")
server = str(private.get("base_url") or resolved.base_url).rstrip("/")
if "127.0.0.1" in server or "localhost" in server:
    server = os.environ.get("VIKNOW_MINERU_VL_SERVER", "https://ai.qingxiang.tech:8443/v1").strip().rstrip("/")
elif server.startswith("http://") and ":8443" in server:
    server = "https://" + server[len("http://") :]
os.environ.setdefault("MINERU_VL_SERVER", server)
api_key = str(private.get("api_key") or resolved.api_key or "").strip()
if api_key:
    os.environ.setdefault("MINERU_VL_API_KEY", api_key)
    os.environ.setdefault("OPENAI_API_KEY", api_key)
os.environ.setdefault("MINERU_VL_MODEL_NAME", "vllm/mineru")
os.environ.setdefault("HYPERRAG_MINERU_VL_MODEL_NAME", "vllm/mineru")
for key in ("MINERU_VL_SERVER", "MINERU_VL_API_KEY", "OPENAI_API_KEY", "MINERU_VL_MODEL_NAME", "HYPERRAG_MINERU_VL_MODEL_NAME", "VIKNOW_MINERU_VL_SERVER"):
    value = os.environ.get(key)
    if value:
        print(f"export {key}={shlex.quote(value)}")
PY
)"
}

export_mineru_vlm_env

API_LOG="${VIKNOW_API_LOG:-/tmp/viknow-api.log}"
UI_LOG="${VIKNOW_UI_LOG:-/tmp/viknow-ui.log}"

python3 -m viknow3 >>"$API_LOG" 2>&1 &
api_pid=$!

ready=0
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_PORT:-5218}/docs"; then
    ready=1
    break
  fi
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "API exited early; log:" >&2
    tail -40 "$API_LOG" >&2 || true
    exit 1
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  echo "API failed to become ready" >&2
  tail -40 "$API_LOG" >&2 || true
  exit 1
fi
echo "ViKnow API ready on :${VIKNOW_PORT:-5218} (pid $api_pid)"

# Playground UI: only ui.env keys + optional app key from secrets (see deploy/236/env/ENV.md).
exec env \
  PYTHONPATH="$PYTHONPATH" \
  VIKNOW_UI_HOST="${VIKNOW_UI_HOST:-0.0.0.0}" \
  VIKNOW_UI_PORT="${VIKNOW_UI_PORT:-5198}" \
  VIKNOW_PLAYGROUND_API_URL="${VIKNOW_PLAYGROUND_API_URL:-http://127.0.0.1:5218}" \
  VIKNOW3_APP_KEY="${VIKNOW3_APP_KEY:-}" \
  python3 -m ui.server >>"$UI_LOG" 2>&1
