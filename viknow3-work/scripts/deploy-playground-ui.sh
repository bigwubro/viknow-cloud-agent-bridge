#!/usr/bin/env bash
# Fast deploy: Playground UI + knowledge BFF on 236 host (no container rebuild).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${ROOT}/src:${ROOT}"
export VIKNOW3_ROOT="$ROOT"
export VIKNOW_HYPERRAG_REPO_ROOT="${VIKNOW_HYPERRAG_REPO_ROOT:-$ROOT/external/HyperRAG}"
export VIKNOW_STATE_DIR="${VIKNOW_STATE_DIR:-/home/cursor-agent/viknow3/state}"
# Host playground runs outside Docker; normalize Actio URLs from container-oriented deploy.env.
if [[ -n "${ACTIO_SERVER_URL:-}" ]]; then
  export ACTIO_SERVER_URL="${ACTIO_SERVER_URL//host.docker.internal/127.0.0.1}"
fi
if [[ "${ACTIO_SERVER_URL:-}" == *":8871"* ]]; then
  export ACTIO_SERVER_URL="${ACTIO_SERVER_URL//:8871/:8600}"
fi
# Host playground: local fast-ingest LLM (8443 edge may 502); Actio VLM caption uses this.
export VIKNOW_PRIVATE_BASE_URL="${VIKNOW_PRIVATE_BASE_URL:-https://127.0.0.1:8443/v1}"
export VIKNOW_PRIVATE_MODEL="${VIKNOW_PRIVATE_MODEL:-Qwen/Qwen3.6-35B-A3B-FP8}"
if [[ "${VIKNOW_PRIVATE_BASE_URL:-}" == http://*":8443"* ]]; then
  export VIKNOW_PRIVATE_BASE_URL="https://${VIKNOW_PRIVATE_BASE_URL#http://}"
fi
# deploy.env carries VIKNOW_PRIVATE_* (same Qwen/LiteLLM path as viknow2-test :5175).
export VIKNOW3_ENV_FILE="${VIKNOW3_ENV_FILE:-/home/cursor-agent/viknow3/pg-demo.env:/home/cursor-agent/deploy.env:/home/cursor-agent/.viknow3/ops.env}"
export VIKNOW3_APP_KEY="${VIKNOW3_APP_KEY:-$(grep -m1 '^VIKNOW3_APP_KEY=' /home/cursor-agent/viknow3/demo.env 2>/dev/null | cut -d= -f2-)}"
export VIKNOW_UI_HOST="${VIKNOW_UI_HOST:-0.0.0.0}"
PLAYGROUND_DATA_ROOT="${VIKNOW_PLAYGROUND_DATA_ROOT:-/data/viknow/knowledge/libraries/playground-cursor-agent}"
export VIKNOW3_DATA_DIR="${VIKNOW3_DATA_DIR:-/data/viknow}"
export VIKNOW3_LIBRARIES_ROOT="${VIKNOW3_LIBRARIES_ROOT:-$PLAYGROUND_DATA_ROOT}"
export VIKNOW_SCRATCH_DIR="${VIKNOW_SCRATCH_DIR:-$PLAYGROUND_DATA_ROOT/scratch}"
export VIKNOW_AGENT_WORKSPACE_ROOT="${VIKNOW_AGENT_WORKSPACE_ROOT:-${VIKNOW_SCRATCH_DIR}/agent-workspaces}"

load_env_files() {
  local f
  IFS=':' read -ra _viknow_env_files <<< "${VIKNOW3_ENV_FILE}"
  for f in "${_viknow_env_files[@]}"; do
    [[ -n "$f" && -f "$f" ]] || continue
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
  done
}

ensure_mineru_for_hyperrag() {
  local bin_dir="${VIKNOW_MINERU_BIN_DIR:-$HOME/viknow3/bin}"
  mkdir -p "$bin_dir"
  ln -sfn "$ROOT/scripts/mineru-docker-wrapper" "$bin_dir/mineru"
  export PATH="$bin_dir:${PATH}"
  export VIKNOW_E2E_MINERU_WRAPPER=1
}

export_mineru_vlm_env() {
  eval "$("$PYTHON" - <<'PY'
import os
import shlex

from viknow3.ops.config.model_resolve import resolve_openai_chat
from viknow3.ops.config.runtime import load_runtime_bundle

private = load_runtime_bundle().providers.get("private") or {}
resolved = resolve_openai_chat("server-default")
server = str(private.get("base_url") or resolved.base_url).rstrip("/")
if "127.0.0.1" in server or "localhost" in server:
    server = os.environ.get(
        "VIKNOW_MINERU_VL_SERVER",
        "https://ai.qingxiang.tech:8443/v1",
    ).strip().rstrip("/")
elif server.startswith("http://") and ":8443" in server:
    server = "https://" + server[len("http://") :]
os.environ.setdefault("MINERU_VL_SERVER", server)
api_key = str(private.get("api_key") or resolved.api_key or "").strip()
if api_key:
    os.environ.setdefault("MINERU_VL_API_KEY", api_key)
    os.environ.setdefault("OPENAI_API_KEY", api_key)
os.environ.setdefault("MINERU_VL_MODEL_NAME", "vllm/mineru")
os.environ.setdefault("HYPERRAG_MINERU_VL_MODEL_NAME", "vllm/mineru")
for key in (
    "MINERU_VL_SERVER",
    "MINERU_VL_API_KEY",
    "OPENAI_API_KEY",
    "MINERU_VL_MODEL_NAME",
    "HYPERRAG_MINERU_VL_MODEL_NAME",
    "VIKNOW_MINERU_VL_SERVER",
):
    value = os.environ.get(key)
    if value:
        print(f"export {key}={shlex.quote(value)}")
PY
)"
}

ensure_playground_data_root() {
  local lib_root="$VIKNOW3_LIBRARIES_ROOT"
  local scratch="$VIKNOW_SCRATCH_DIR"
  if [[ -w "$scratch" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "scratch dir not writable: $scratch (install docker or chown $lib_root to cursor-agent)" >&2
    return 1
  fi
  docker run --rm -v /data/viknow/knowledge/libraries:/mnt alpine     sh -c "mkdir -p /mnt/playground-cursor-agent/scratch && chown -R 1012:1013 /mnt/playground-cursor-agent" || {
    echo "failed to provision $lib_root via docker" >&2
    return 1
  }
}
export VIKNOW_PORT="${VIKNOW_PORT:-5175}"
export VIKNOW_UI_PORT="${VIKNOW_UI_PORT:-5198}"
export VIKNOW_PLAYGROUND_API_URL="${VIKNOW_PLAYGROUND_API_URL:-http://127.0.0.1:${VIKNOW_PORT}}"
export VIKNOW_CORS_ORIGINS="${VIKNOW_CORS_ORIGINS:-http://127.0.0.1:${VIKNOW_UI_PORT}}"
# deepagents needs Python >= 3.11; the 236 host python3 is 3.10, so the UI runs from this venv.
PYTHON="${VIKNOW_PYTHON:-/home/cursor-agent/viknow3/venv312/bin/python}"

API_PIDFILE="${TMPDIR:-/tmp}/viknow-playground-api.pid"
UI_PIDFILE="${TMPDIR:-/tmp}/viknow-playground-ui.pid"
API_LOG="${TMPDIR:-/tmp}/viknow-api.log"
LOG="${TMPDIR:-/tmp}/viknow-ui.log"

stop_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    sleep 0.5
  fi
}

stop_pidfile() {
  local file="$1"
  if [[ -f "$file" ]]; then
    old="$(cat "$file" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_ui() {
  stop_port "${VIKNOW_UI_PORT}"
  stop_port "${VIKNOW_PORT}"
  stop_pidfile "$UI_PIDFILE"
  stop_pidfile "$API_PIDFILE"
}

ensure_s3_public_endpoint() {
  # Set only after load_env_files so we do not pin 127.0.0.1 before deploy.env (viknow2-test uses 36.103…:19191).
  # viknow3 storage/s3.py prefers this env over ops public_endpoint; wrong value breaks remote-browser PUT.
  if [[ -z "${VIKNOW_S3_PUBLIC_ENDPOINT:-}" ]]; then
    export VIKNOW_S3_PUBLIC_ENDPOINT="http://36.103.198.236:19191"
  fi
}

start_api() {
  nohup env PYTHONPATH="${PYTHONPATH}" VIKNOW3_ROOT="${VIKNOW3_ROOT}" VIKNOW3_ENV_FILE="${VIKNOW3_ENV_FILE:-}" \
    VIKNOW_HOST="${VIKNOW_HOST:-0.0.0.0}" VIKNOW_PORT="${VIKNOW_PORT}" VIKNOW_CORS_ORIGINS="${VIKNOW_CORS_ORIGINS}" \
    JIEBA_CACHE_DIR="${JIEBA_CACHE_DIR}" TMPDIR="${TMPDIR}" \
    "$PYTHON" -m viknow3.server >>"$API_LOG" 2>&1 &
  echo $! >"$API_PIDFILE"
  ready=0
  for _ in $(seq 1 45); do
    if curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_PORT}/docs"; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" != 1 ]]; then
    echo "ViKnow API failed to start; tail $API_LOG"
    tail -20 "$API_LOG" || true
    exit 1
  fi
  echo "ViKnow API http://127.0.0.1:${VIKNOW_PORT} (pid $(cat "$API_PIDFILE"))"
}

start_ui() {
  ensure_playground_data_root
  mkdir -p "$VIKNOW_SCRATCH_DIR" "$VIKNOW_AGENT_WORKSPACE_ROOT" "$VIKNOW3_LIBRARIES_ROOT"
  load_env_files
  ensure_s3_public_endpoint
  ensure_mineru_for_hyperrag
  export_mineru_vlm_env
  mkdir -p "${VIKNOW_SCRATCH_DIR}/jieba-cache"
  export JIEBA_CACHE_DIR="${JIEBA_CACHE_DIR:-${VIKNOW_SCRATCH_DIR}/jieba-cache}"
  export TMPDIR="${TMPDIR:-${VIKNOW_SCRATCH_DIR}/tmp}"
  mkdir -p "$TMPDIR"
  stop_ui
  "$PYTHON" -c "import deepagents, deepagents_sandbox, langchain_openai, langgraph.checkpoint.postgres, psycopg_pool, google.adk" || {
    echo "$PYTHON lacks the agent dependencies; install with: $PYTHON -m pip install -e $ROOT && $PYTHON -m pip install -e /home/cursor-agent/work/viknow2/external/deepagents-sandbox[langchain]"
    exit 1
  }
  start_api
  nohup env PYTHONPATH="${PYTHONPATH}" VIKNOW3_ROOT="${VIKNOW3_ROOT}" \
    VIKNOW_UI_HOST="${VIKNOW_UI_HOST:-0.0.0.0}" VIKNOW_UI_PORT="${VIKNOW_UI_PORT}" \
    VIKNOW_PLAYGROUND_API_URL="${VIKNOW_PLAYGROUND_API_URL}" \
    VIKNOW_STORAGE_URL="${VIKNOW_STORAGE_URL:-}" VIKNOW_STORAGE_TOKEN="${VIKNOW_STORAGE_TOKEN:-}" \
    VIKNOW3_APP_KEY="${VIKNOW3_APP_KEY:-}" \
    "$PYTHON" -m ui.server >>"$LOG" 2>&1 &
  echo $! >"$UI_PIDFILE"
  ready=0
  for _ in $(seq 1 45); do
    if curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_UI_PORT}/playground"; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" != 1 ]]; then
    echo "Playground failed to start; tail $LOG"
    tail -20 "$LOG" || true
    exit 1
  fi
  if [[ -f "$ROOT/scripts/set-playground-s3-cors.py" ]] && [[ -n "${VIKNOW_OPS_DSN:-}" ]]; then
    "$PYTHON" "$ROOT/scripts/set-playground-s3-cors.py" || echo "warn: bucket CORS setup failed (browser presign PUT may fail)" >&2
  fi
  echo "Playground UI http://127.0.0.1:${VIKNOW_UI_PORT}/playground (pid $(cat "$UI_PIDFILE"))"
}

case "${1:-restart}" in
  stop) stop_ui ;;
  start) start_ui ;;
  restart) start_ui ;;
  status)
    if [[ -f "$API_PIDFILE" ]] && kill -0 "$(cat "$API_PIDFILE")" 2>/dev/null; then
      echo "api running pid $(cat "$API_PIDFILE") :${VIKNOW_PORT}"
    else
      echo "api stopped"
    fi
    if [[ -f "$UI_PIDFILE" ]] && kill -0 "$(cat "$UI_PIDFILE")" 2>/dev/null; then
      echo "ui running pid $(cat "$UI_PIDFILE") :${VIKNOW_UI_PORT}"
    else
      echo "ui stopped"
    fi
    ;;
  *) echo "usage: $0 [restart|start|stop|status]" >&2; exit 2 ;;
esac
