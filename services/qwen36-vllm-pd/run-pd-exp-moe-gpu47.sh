#!/usr/bin/env bash
# Isolated 1P+1D on GPU 4+7 for MoE backend A/B (default vs flashinfer_cutlass).
# Does not touch qwen36-p0..d3, :8500, or viknow2-test.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.29.0}"
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
SERVED=Qwen/Qwen3.6-35B-A3B-FP8
SPEC='{"method":"mtp","num_speculative_tokens":1}'
PAIR_GPUS=4,7
P_PORT=8710
D_PORT=8711
PROXY_PORT=8720
NIXL_P=5710
NIXL_D=5711
NAME_P=qwen36-exp-p47
NAME_D=qwen36-exp-d47
PROXY_PID=/tmp/qwen36-exp-moe-proxy.pid
PROXY_LOG=/tmp/qwen36-exp-moe-proxy.log
RESULT_DIR=/tmp/qwen36-moe-exp-$(date -u +%Y%m%d)

UCX_INTRANODE_TLS=sm,self,cuda_ipc,cuda_copy

common_args() {
  local moe_extra=()
  if [[ -n "${MOE_BACKEND:-}" ]]; then
    moe_extra=(--moe-backend "${MOE_BACKEND}")
  fi
  COMMON=(
    "${MODEL}"
    --host 127.0.0.1
    --served-model-name "${SERVED}"
    --tensor-parallel-size 1
    --kv-cache-dtype fp8
    --enable-prefix-caching
    --mamba-cache-mode align
    --max-model-len 65536
    --enable-auto-tool-choice
    --tool-call-parser qwen3_coder
    --async-scheduling
    --reasoning-parser qwen3
    --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
    --default-chat-template-kwargs '{"enable_thinking": false}'
    --speculative-config "${SPEC}"
    --log-error-stack
    --uvicorn-log-level warning
    "${moe_extra[@]}"
  )
}

start_engine() {
  local name="$1" cuda_visible="$2" port="$3" role="$4" nixl_port="$5"
  local kv extra=()
  common_args
  if [[ "$role" == p ]]; then
    kv='{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"kv_lease_duration":120,"num_threads":8}}'
    extra+=(--gpu-memory-utilization 0.80 --kv-cache-memory 17179869184 --scheduling-policy fcfs --max-num-seqs 64 --max-num-batched-tokens 32768)
  else
    kv='{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"num_threads":8}}'
    extra+=(--gpu-memory-utilization 0.80 --max-num-seqs 64 --max-num-batched-tokens 16384)
  fi
  docker rm -f "$name" 2>/dev/null || true
  docker run -d \
    --name "$name" \
    --gpus '"device='"${PAIR_GPUS}"'"' \
    --network host \
    --ipc host \
    --pid host \
    --cap-add SYS_PTRACE \
    --security-opt seccomp=unconfined \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    --device /dev/nvidia-caps/nvidia-cap1 \
    --device /dev/nvidia-caps/nvidia-cap2 \
    -v /data/twj/models:/root/.cache/models:ro \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.12/sitecustomize.py:ro" \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.13/sitecustomize.py:ro" \
    -e NVIDIA_VISIBLE_DEVICES="${PAIR_GPUS}" \
    -e PYTHONHASHSEED=0 \
    -e PYTHONUNBUFFERED=1 \
    -e VLLM_USE_DEEP_GEMM=0 \
    -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
    -e VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.1 \
    -e VLLM_NIXL_SIDE_CHANNEL_PORT="${nixl_port}" \
    -e VLLM_SSM_CONV_STATE_LAYOUT=DS \
    -e CUDA_VISIBLE_DEVICES="${cuda_visible}" \
    -e UCX_TLS="${UCX_INTRANODE_TLS}" \
    -e UCX_MEMTYPE_CACHE=n \
    -e UCX_RNDV_SCHEME=put_zcopy \
    -e UCX_RNDV_THRESH=0 \
    -e UCX_PROTO_ENABLE=y \
    -e UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on \
    -e UCX_CUDA_IPC_BW=50000MBs \
    -e CUDA_DEVICE_MAX_CONNECTIONS=8 \
    "$IMAGE" \
    "${COMMON[@]}" \
    --port "${port}" \
    --kv-transfer-config "${kv}" \
    "${extra[@]}"
  echo "started ${name} moe=${MOE_BACKEND:-auto} port=${port} role=${role}"
}

wait_http() {
  local url="$1" n=0
  while (( n < 120 )); do
    if curl -sf -m 3 "$url" >/dev/null; then
      echo "ready ${url}"
      return 0
    fi
    sleep 5
    n=$((n + 1))
  done
  echo "timeout ${url}" >&2
  return 1
}

stop_proxy() {
  if [[ -f "$PROXY_PID" ]]; then
    local pid
    pid="$(cat "$PROXY_PID" || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      sleep 1
    fi
    rm -f "$PROXY_PID"
  fi
}

cmd_prepare() {
  echo "Stopping GPU4 Qwen3.8 and GPU7 fast-ingest + embed-vl (temporary for exp)"
  docker stop vllm-qwen38-gpu4 vllm-fast-ingest-llm vllm-embed-vl-g7-a 2>/dev/null || true
  sleep 3
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed -n '5,8p'
}

cmd_restore_aux() {
  echo "Restoring aux + Qwen3.8 stopped for exp"
  docker start vllm-fast-ingest-llm vllm-embed-vl-g7-a vllm-qwen38-gpu4 2>/dev/null || true
}

cmd_stop() {
  stop_proxy
  docker rm -f "$NAME_P" "$NAME_D" 2>/dev/null || true
}

resolve_moe_backend() {
  local mode="${1:-default}"
  case "$mode" in
    default|auto|triton) unset MOE_BACKEND ;;
    flashinfer_cutlass|cutlass-fi) export MOE_BACKEND=flashinfer_cutlass ;;
    cutlass|vllm_cutlass) export MOE_BACKEND=cutlass ;;
    *) export MOE_BACKEND="$mode" ;;
  esac
}

wait_d_or_fail() {
  local n=0
  while (( n < 90 )); do
    if curl -sf -m 3 "http://127.0.0.1:${D_PORT}/v1/models" >/dev/null; then
      echo "ready D ${MOE_BACKEND:-auto}"
      return 0
    fi
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME_D"; then
      echo "D container exited (${MOE_BACKEND:-auto})" >&2
      docker logs "$NAME_D" 2>&1 | grep -iE 'ValueError|RuntimeError|does not support|FLASHINFER|moe_backend' | tail -5 >&2 || true
      return 1
    fi
    sleep 5
    n=$((n + 1))
  done
  echo "timeout D (${MOE_BACKEND:-auto})" >&2
  return 1
}

cmd_probe_d() {
  local mode="${1:-default}"
  resolve_moe_backend "$mode"
  docker rm -f "$NAME_D" 2>/dev/null || true
  echo "=== probe D only: ${MOE_BACKEND:-auto/triton} ==="
  start_engine "$NAME_D" 1,0 "$D_PORT" d "$NIXL_D"
  if wait_d_or_fail; then
    docker logs "$NAME_D" 2>&1 | grep -iE 'moe|triton|flashinfer|cutlass|deep_gemm|marlin' | tail -8 || true
    return 0
  fi
  return 1
}

cmd_start() {
  local mode="${1:-default}"
  cmd_stop
  resolve_moe_backend "$mode"
  echo "=== MoE mode: ${MOE_BACKEND:-auto/triton} ==="
  # Start D before P so GPU7 claims memory before P pins IPC peers.
  start_engine "$NAME_D" 1,0 "$D_PORT" d "$NIXL_D"
  wait_http "http://127.0.0.1:${D_PORT}/v1/models" || return 1
  start_engine "$NAME_P" 0,1 "$P_PORT" p "$NIXL_P"
  wait_http "http://127.0.0.1:${P_PORT}/v1/models" || return 1
  stop_proxy
  nohup python3 "${DIR}/pd_pair_proxy.py" --port "${PROXY_PORT}" \
    --prefill "http://127.0.0.1:${P_PORT}" \
    --decode "http://127.0.0.1:${D_PORT}" \
    >"$PROXY_LOG" 2>&1 &
  echo $! >"$PROXY_PID"
  sleep 2
  wait_http "http://127.0.0.1:${PROXY_PORT}/health" || return 1
  mkdir -p "$RESULT_DIR"
  docker logs "$NAME_D" 2>&1 | tail -80 | tee "${RESULT_DIR}/d-startup-${mode}.log" | grep -iE 'moe|triton|flashinfer|FlashInfer|cutlass|deep_gemm|marlin' || true
  return 0
}

cmd_bench() {
  local label="${1:-run}"
  mkdir -p "$RESULT_DIR"
  local out="${RESULT_DIR}/bench-${label}.json"
  python3 - <<'PY' "$out" "$PROXY_PORT"
import json, sys, time, urllib.request
out, port = sys.argv[1], int(sys.argv[2])
base = f"http://127.0.0.1:{port}/v1/chat/completions"
prompt = "用一句话总结下面内容。\n" + ("机器学习与推理优化。" * 800)
body = {
    "model": "Qwen/Qwen3.6-35B-A3B-FP8",
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 128,
    "temperature": 0,
}
data = json.dumps(body).encode()

def one(i):
    t0 = time.perf_counter()
    req = urllib.request.Request(base, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        json.load(r)
    return time.perf_counter() - t0

# warmup
one(0)
lat = []
t0 = time.perf_counter()
for i in range(12):
    lat.append(one(i))
wall = time.perf_counter() - t0
lat.sort()
res = {
    "n": len(lat),
    "wall_s": wall,
    "qps": len(lat) / wall,
    "p50_s": lat[len(lat) // 2],
    "p95_s": lat[int(len(lat) * 0.95)],
    "prompt_chars": len(prompt),
    "max_tokens": 128,
}
print(json.dumps(res, indent=2))
open(out, "w").write(json.dumps(res, indent=2))
PY
  echo "wrote $out"
  curl -sf "http://127.0.0.1:${D_PORT}/metrics" 2>/dev/null | grep -E 'generation_tokens_total|spec_decode.*accept' | tail -5 || true
}

cmd_compare() {
  cmd_prepare
  cmd_start default
  cmd_bench "default"
  cmd_stop
  sleep 5
  cmd_start flashinfer_cutlass
  cmd_bench "flashinfer_cutlass" || true
  cmd_stop
  cmd_restore_aux
  echo "Results in ${RESULT_DIR}"
  ls -la "${RESULT_DIR}"
}

cmd_sweep() {
  mkdir -p "$RESULT_DIR"
  local summary="${RESULT_DIR}/sweep-summary.txt"
  : >"$summary"
  cmd_prepare
  local backends=(default cutlass deep_gemm flashinfer_trtllm marlin flashinfer_cutlass)
  for b in "${backends[@]}"; do
    echo "----- $b -----" | tee -a "$summary"
    if ! cmd_start "$b"; then
      echo "${b}: PD_START_FAIL" | tee -a "$summary"
      cmd_stop
      sleep 3
      continue
    fi
    if cmd_bench "$b"; then
      echo "${b}: OK $(cat "${RESULT_DIR}/bench-${b}.json" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f"qps={d[\"qps\"]:.3f} p50={d[\"p50_s\"]:.3f}")' 2>/dev/null || echo)" | tee -a "$summary"
    else
      echo "${b}: BENCH_FAIL" | tee -a "$summary"
    fi
    cmd_stop
    sleep 5
  done
  cmd_restore_aux
  echo "Sweep done: $summary"
  cat "$summary"
}

usage() {
  echo "Usage: $0 {prepare|restore-aux|stop|probe-d <backend>|start <backend>|bench [label]|compare|sweep}"
  echo "Backends: default, cutlass (vLLM), deep_gemm, flashinfer_trtllm, marlin, flashinfer_cutlass"
}

case "${1:-}" in
  prepare) cmd_prepare ;;
  restore-aux) cmd_restore_aux ;;
  stop) cmd_stop ;;
  probe-d) cmd_probe_d "${2:-default}" ;;
  start) cmd_start "${2:-default}" ;;
  bench) cmd_bench "${2:-run}" ;;
  compare) cmd_compare ;;
  sweep) cmd_sweep ;;
  *) usage; exit 2 ;;
esac
