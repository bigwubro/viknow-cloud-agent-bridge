#!/bin/bash
# Workload B only. Default is plan (no traffic).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/workload_b_compare.py"
OUT="${VIKNOW_OUT_DIR:-$DIR/out}"
export VIKNOW_OUT_DIR="$OUT"
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
  *)
    echo "usage: $0 {plan|estimate|local|sf-align|sf-full|report|demo}"
    exit 2
    ;;
esac
