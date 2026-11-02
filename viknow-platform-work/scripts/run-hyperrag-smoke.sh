#!/usr/bin/env bash
# Start viknow-platform with HyperRAG (config from viknow_ops only).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OPS_ENV="${VIKNOW_OPS_ENV:-$HOME/.viknow-platform/ops.env}"
if [[ -f "$OPS_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OPS_ENV"
  set +a
fi

if [[ -z "${VIKNOW_OPS_DSN:-}" ]]; then
  echo "VIKNOW_OPS_DSN is required (HyperRAG reads viknow_ops, not YAML)." >&2
  exit 1
fi

export PYTHONPATH="$ROOT/src"
export VIKNOW_HOST="${VIKNOW_HOST:-127.0.0.1}"
export VIKNOW_PORT="${VIKNOW_PORT:-5199}"
export VIKNOW_HYPERRAG_REPO_ROOT="${VIKNOW_HYPERRAG_REPO_ROOT:-$ROOT/external/HyperRAG}"
export VIKNOW_STATE_DIR="${VIKNOW_STATE_DIR:-/tmp/viknow-platform-state}"

echo "Materialize from ops..."
python3 - <<'PY'
from viknow_platform.application.hyperrag.config import load_app_config, materialize_hyperrag_config

cfg = load_app_config()
path = materialize_hyperrag_config(cfg)
print("materialized:", path)
PY

echo "Starting on $VIKNOW_HOST:$VIKNOW_PORT (Ctrl+C to stop)..."
exec python3 -m viknow_platform
