#!/bin/bash
# Workload B only. Default is plan (no traffic).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/workload_b_compare.py"
OUT="${VIKNOW_OUT_DIR:-$DIR/out}"
export VIKNOW_OUT_DIR="$OUT"
export LLM_TARGET_PROMPT_TOKENS="${LLM_TARGET_PROMPT_TOKENS:-30000}"
export LLM_UNIQUE_TOKENS="${LLM_UNIQUE_TOKENS:-13500}"
if [ -z "${SILICONFLOW_API_KEY:-}" ] && [ -f /home/cursor-agent/.secrets/sf_probe.key ]; then
  SILICONFLOW_API_KEY="$(tr -d '\n' < /home/cursor-agent/.secrets/sf_probe.key)"
  export SILICONFLOW_API_KEY
fi
mkdir -p "$OUT"

cmd="${1:-plan}"
shift || true

case "$cmd" in
  plan)
    python3 -u "$PY" plan
    python3 -u "$PY" estimate
    ;;
  estimate)
    python3 -u "$PY" estimate "$@"
    ;;
  local)
    python3 -u "$PY" run --target local --concurrency 1,8,32,80 --duration 600 "$@"
    ;;
  sf-align)
    python3 -u "$PY" run --target sf --concurrency 1,8 --duration 600 --i-accept-sf-cost "$@"
    ;;
  sf-full)
    python3 -u "$PY" run --target sf --concurrency 1,8,32,80 --duration 600 --i-accept-sf-cost "$@"
    ;;
  report)
    python3 -u "$PY" report --dir "$OUT" "$@"
    ;;
  demo)
    python3 -u "$PY" report --demo-morning
    ;;
  probe)
    python3 -u "$DIR/probe_sf.py"
    ;;
  *)
    echo "usage: $0 {plan|estimate|local|sf-align|sf-full|report|demo|probe}"
    exit 2
    ;;
esac
