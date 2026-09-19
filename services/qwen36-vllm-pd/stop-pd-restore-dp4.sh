#!/usr/bin/env bash
# Stop the 1P+1D experiment and bring back DP4 vllm-vlm on :8500.
set -euo pipefail
stop_pidfile() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local pid
    pid="$(cat "$f" || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
    fi
    rm -f "$f"
  fi
}
stop_pidfile /tmp/qwen36-pd-proxy-a.pid
stop_pidfile /tmp/qwen36-pd-proxy-b.pid
stop_pidfile /tmp/qwen36-pd-proxy-3p1d.pid
docker rm -f qwen36-pd-lb qwen36-p0 qwen36-p1 qwen36-d1 qwen36-p2 qwen36-d3 \
  qwen36-lmc-coord qwen36-lmc-p0 qwen36-lmc-p1 qwen36-lmc-p2 qwen36-lmc-d3 \
  qwen36-mooncake-master 2>/dev/null || true
docker start vllm-vlm
echo "restored vllm-vlm; wait for :8500"
for i in $(seq 1 60); do
  if curl -sf -m 3 http://127.0.0.1:8500/v1/models >/dev/null; then
    echo ready
    exit 0
  fi
  sleep 5
done
echo "vllm-vlm started but /v1/models not up yet"
exit 1
