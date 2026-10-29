#!/usr/bin/env bash
# Playground on 236: thin UI (:5198) + ViKnow access API (:5218). HyperRAG/agent env only on API.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${ROOT}/src:${ROOT}"
export VIKNOW_PLATFORM_ROOT="$ROOT"
export VIKNOW_HYPERRAG_REPO_ROOT="${VIKNOW_HYPERRAG_REPO_ROOT:-$ROOT/external/HyperRAG}"
export VIKNOW_STATE_DIR="${VIKNOW_STATE_DIR:-/home/cursor-agent/viknow-platform/state}"
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
export VIKNOW_ENV_FILE="${VIKNOW_ENV_FILE:-/home/cursor-agent/viknow-platform/pg-demo.env:/home/cursor-agent/viknow3/pg-demo.env:/home/cursor-agent/deploy.env:/home/cursor-agent/.viknow-platform/ops.env:/home/cursor-agent/.viknow3/ops.env}"
export VIKNOW_APP_KEY="${VIKNOW_APP_KEY:-$(grep -m1 '^VIKNOW_APP_KEY=' /home/cursor-agent/viknow-platform/demo.env 2>/dev/null | cut -d= -f2-)}"
export VIKNOW_APP_KEY="${VIKNOW_APP_KEY:-$(grep -m1 '^VIKNOW_APP_KEY=' /home/cursor-agent/viknow3/demo.env 2>/dev/null | cut -d= -f2-)}"
export VIKNOW_APP_KEY="${VIKNOW_APP_KEY:-$(grep -m1 '^VIKNOW3_APP_KEY=' /home/cursor-agent/viknow3/demo.env 2>/dev/null | cut -d= -f2-)}"
export VIKNOW_UI_HOST="${VIKNOW_UI_HOST:-0.0.0.0}"
export VIKNOW_DATA_DIR="${VIKNOW_DATA_DIR:-/data/viknow}"
export VIKNOW_LIBRARIES_ROOT="${VIKNOW_LIBRARIES_ROOT:-${VIKNOW_DATA_DIR}/hyperrag-wd}"
export VIKNOW_SCRATCH_DIR="${VIKNOW_SCRATCH_DIR:-${VIKNOW_DATA_DIR}/scratch}"
export VIKNOW_AGENT_WORKSPACE_ROOT="${VIKNOW_AGENT_WORKSPACE_ROOT:-${VIKNOW_DATA_DIR}/agent-workspaces}"

DEPLOY236_ENV="${ROOT}/deploy/236/env"
API_ENV_FILE="${VIKNOW_API_ENV_FILE:-$DEPLOY236_ENV/api.env}"
UI_ENV_FILE="${VIKNOW_UI_ENV_FILE:-$DEPLOY236_ENV/ui.env}"
SECRETS_ENV_FILE="${VIKNOW_SECRETS_ENV_FILE:-$DEPLOY236_ENV/secrets.env}"

load_env_files() {
  local f
  IFS=':' read -ra _viknow_env_files <<< "${VIKNOW_ENV_FILE}"
  for f in "${_viknow_env_files[@]}"; do
    [[ -n "$f" && -f "$f" ]] || continue
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
  done
  apply_legacy_env_aliases
}

apply_legacy_env_aliases() {
  # Host files not yet rewritten after platform rename (removed once finish-platform-migration-236.sh has run).
  [[ -z "${VIKNOW_APP_KEY:-}" && -n "${VIKNOW3_APP_KEY:-}" ]] && export VIKNOW_APP_KEY="$VIKNOW3_APP_KEY"
  [[ -z "${VIKNOW_DATA_DIR:-}" && -n "${VIKNOW3_DATA_DIR:-}" ]] && export VIKNOW_DATA_DIR="$VIKNOW3_DATA_DIR"
  [[ -z "${VIKNOW_LIBRARIES_ROOT:-}" && -n "${VIKNOW3_LIBRARIES_ROOT:-}" ]] && export VIKNOW_LIBRARIES_ROOT="$VIKNOW3_LIBRARIES_ROOT"
  [[ -z "${VIKNOW_PLATFORM_ROOT:-}" && -n "${VIKNOW3_ROOT:-}" ]] && export VIKNOW_PLATFORM_ROOT="$VIKNOW3_ROOT"
}

load_api_env() {
  set -a
  if [[ -f "$API_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$API_ENV_FILE"
  else
    echo "warn: missing $API_ENV_FILE (see deploy/236/env/ENV.md)" >&2
  fi
  if [[ -f "$SECRETS_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_ENV_FILE"
  fi
  load_env_files
  set +a
  # api.env uses /state for the Docker mount; on host Playground map to a writable tree.
  if [[ "${VIKNOW_STATE_DIR:-}" == "/state" ]] && [[ ! -w "/state" ]]; then
    export VIKNOW_STATE_DIR="/home/cursor-agent/viknow-platform/state"
  fi
  if [[ "${VIKNOW_VENDOR_FILES_ROOT:-}" == "/state/vendor-files" ]] && [[ ! -w "/state" ]]; then
    export VIKNOW_VENDOR_FILES_ROOT="${VIKNOW_STATE_DIR}/vendor-files"
  fi
}

load_ui_env() {
  set -a
  if [[ -f "$UI_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$UI_ENV_FILE"
  else
    echo "warn: missing $UI_ENV_FILE (see deploy/236/env/ENV.md)" >&2
  fi
  if [[ -f "$SECRETS_ENV_FILE" ]]; then
    local key_line token_line
    key_line="$(grep -m1 '^VIKNOW_APP_KEY=' "$SECRETS_ENV_FILE" 2>/dev/null || grep -m1 '^VIKNOW3_APP_KEY=' "$SECRETS_ENV_FILE" 2>/dev/null || true)"
    if [[ -n "$key_line" ]]; then
      VIKNOW_APP_KEY="${key_line#*=}"
      export VIKNOW_APP_KEY
    fi
    token_line="$(grep -m1 '^VIKNOW_STORAGE_TOKEN=' "$SECRETS_ENV_FILE" 2>/dev/null || true)"
    if [[ -n "$token_line" ]]; then
      VIKNOW_STORAGE_TOKEN="${token_line#*=}"
      export VIKNOW_STORAGE_TOKEN
    fi
  fi
  set +a
}

ensure_mineru_for_hyperrag() {
  local bin_dir="${VIKNOW_MINERU_BIN_DIR:-$HOME/viknow-platform/bin}"
  mkdir -p "$bin_dir"
  ln -sfn "$ROOT/scripts/mineru-docker-wrapper" "$bin_dir/mineru"
  export PATH="$bin_dir:${PATH}"
  export VIKNOW_E2E_MINERU_WRAPPER=1
}

export_mineru_vlm_env() {
  eval "$("$PYTHON" - <<'PY'
import os
import shlex

from viknow_platform.ops.config.model_resolve import resolve_openai_chat
from viknow_platform.ops.config.runtime import load_runtime_bundle

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
  local lib_root="$VIKNOW_LIBRARIES_ROOT"
  local scratch="$VIKNOW_SCRATCH_DIR"
  if [[ -w "$scratch" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "scratch dir not writable: $scratch (install docker or chown $lib_root to cursor-agent)" >&2
    return 1
  fi
  docker run --rm -v /data/viknow:/mnt alpine \
    sh -c "mkdir -p /mnt/scratch /mnt/agent-workspaces /mnt/hyperrag-wd && chown -R 1012:1013 /mnt/scratch /mnt/agent-workspaces /mnt/hyperrag-wd" || {
    echo "failed to provision ${VIKNOW_DATA_DIR} runtime dirs via docker" >&2
    return 1
  }
}
export VIKNOW_UI_PORT="${VIKNOW_UI_PORT:-5198}"
export VIKNOW_API_PORT="${VIKNOW_API_PORT:-5218}"
export VIKNOW_PORT="${VIKNOW_PORT:-$VIKNOW_API_PORT}"
export VIKNOW_HOST="${VIKNOW_HOST:-0.0.0.0}"
export VIKNOW_PLAYGROUND_API_URL="${VIKNOW_PLAYGROUND_API_URL:-http://127.0.0.1:${VIKNOW_API_PORT}}"
# deepagents needs Python >= 3.11; the 236 host python3 is 3.10, so processes run from this venv.
PYTHON="${VIKNOW_PYTHON:-/home/cursor-agent/viknow-platform/venv312/bin/python}"

UI_PIDFILE="${TMPDIR:-/tmp}/viknow-playground-ui.pid"
API_PIDFILE="${TMPDIR:-/tmp}/viknow-playground-api.pid"
UI_LOG="${TMPDIR:-/tmp}/viknow-ui.log"
API_LOG="${TMPDIR:-/tmp}/viknow-api.log"

stop_api() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${VIKNOW_API_PORT}/tcp" >/dev/null 2>&1 || true
    sleep 0.5
  fi
  if [[ -f "$API_PIDFILE" ]]; then
    old="$(cat "$API_PIDFILE" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
    fi
    rm -f "$API_PIDFILE"
  fi
}

stop_ui() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${VIKNOW_UI_PORT}/tcp" >/dev/null 2>&1 || true
    sleep 0.5
  fi
  if [[ -f "$UI_PIDFILE" ]]; then
    old="$(cat "$UI_PIDFILE" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
    fi
    rm -f "$UI_PIDFILE"
  fi
}

start_api() {
  ensure_playground_data_root
  mkdir -p "$VIKNOW_SCRATCH_DIR" "$VIKNOW_AGENT_WORKSPACE_ROOT" "$VIKNOW_LIBRARIES_ROOT"
  load_api_env
  ensure_mineru_for_hyperrag
  export_mineru_vlm_env
  mkdir -p "${VIKNOW_STATE_DIR}" "${VIKNOW_VENDOR_FILES_ROOT}" "${VIKNOW_SCRATCH_DIR}/jieba-cache"
  export JIEBA_CACHE_DIR="${JIEBA_CACHE_DIR:-${VIKNOW_SCRATCH_DIR}/jieba-cache}"
  export TMPDIR="${TMPDIR:-${VIKNOW_SCRATCH_DIR}/tmp}"
  mkdir -p "$TMPDIR"
  stop_api
  "$PYTHON" -c "import deepagents, deepagents_sandbox, langchain_openai, langgraph.checkpoint.postgres, psycopg_pool" || {
    echo "$PYTHON lacks the agent dependencies; install with: $PYTHON -m pip install -e $ROOT && $PYTHON -m pip install -e /home/cursor-agent/work/viknow2/external/deepagents-sandbox[langchain]"
    exit 1
  }
  nohup env PYTHONPATH="${PYTHONPATH}" VIKNOW_PLATFORM_ROOT="${VIKNOW_PLATFORM_ROOT}" VIKNOW_ENV_FILE="${VIKNOW_ENV_FILE:-}" \
    VIKNOW_HOST="${VIKNOW_HOST}" VIKNOW_PORT="${VIKNOW_API_PORT}" \
    JIEBA_CACHE_DIR="${JIEBA_CACHE_DIR}" TMPDIR="${TMPDIR}" \
    "$PYTHON" -m viknow_platform >>"$API_LOG" 2>&1 &
  echo $! >"$API_PIDFILE"
  ready=0
  for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_API_PORT}/docs" || \
       curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_API_PORT}/openapi.json"; then
      ready=1
      break
    fi
    if curl -sf -H "X-App-Key: ${VIKNOW_APP_KEY:-dev-app-key}" -o /dev/null \
      "http://127.0.0.1:${VIKNOW_API_PORT}/api/v1/agent/agents"; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" != 1 ]]; then
    echo "ViKnow API failed to start; tail $API_LOG"
    tail -30 "$API_LOG" || true
    exit 1
  fi
  echo "ViKnow API http://127.0.0.1:${VIKNOW_API_PORT}/api/v1 (pid $(cat "$API_PIDFILE"))"
}

start_ui() {
  load_ui_env
  stop_ui
  nohup env PYTHONPATH="${PYTHONPATH}" \
    VIKNOW_UI_HOST="${VIKNOW_UI_HOST}" VIKNOW_UI_PORT="${VIKNOW_UI_PORT}" \
    VIKNOW_PLAYGROUND_API_URL="${VIKNOW_PLAYGROUND_API_URL}" \
    VIKNOW_STORAGE_URL="${VIKNOW_STORAGE_URL:-}" \
    VIKNOW_STORAGE_TOKEN="${VIKNOW_STORAGE_TOKEN:-}" \
    VIKNOW_APP_KEY="${VIKNOW_APP_KEY:-}" \
    "$PYTHON" -m ui.server >>"$UI_LOG" 2>&1 &
  echo $! >"$UI_PIDFILE"
  ready=0
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "http://127.0.0.1:${VIKNOW_UI_PORT}/playground"; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" != 1 ]]; then
    echo "Playground UI failed to start; tail $UI_LOG"
    tail -20 "$UI_LOG" || true
    exit 1
  fi
  echo "Playground UI http://127.0.0.1:${VIKNOW_UI_PORT}/playground → API ${VIKNOW_PLAYGROUND_API_URL} (pid $(cat "$UI_PIDFILE"))"
}

start_all() {
  start_api
  start_ui
}

case "${1:-restart}" in
  stop)
    stop_ui
    stop_api
    ;;
  start-api) start_api ;;
  start-ui) start_ui ;;
  start) start_all ;;
  restart) start_all ;;
  status)
    if [[ -f "$API_PIDFILE" ]] && kill -0 "$(cat "$API_PIDFILE")" 2>/dev/null; then
      echo "api running pid $(cat "$API_PIDFILE")"
    else
      echo "api stopped"
    fi
    if [[ -f "$UI_PIDFILE" ]] && kill -0 "$(cat "$UI_PIDFILE")" 2>/dev/null; then
      echo "ui running pid $(cat "$UI_PIDFILE")"
    else
      echo "ui stopped"
    fi
    ;;
  *) echo "usage: $0 [restart|start|stop|start-api|start-ui|status]" >&2; exit 2 ;;
esac
