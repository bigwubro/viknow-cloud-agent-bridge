#!/usr/bin/env bash
# EP vs PD baseline on isolated GPUs (does not touch qwen36-p0..d3 / :8500).
#   - collocated: EP2 single engine on GPU 4+7 (no PD)
#   - pd-ep:      EP2 P on 4+5 + EP2 D on 6+7 + Nixl + proxy (PD disaggregated)
#   - compare:    bench collocated EP vs PD+EP vs prior PD-no-EP (run-pd-exp-moe-gpu47)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.29.0}"
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
SERVED=Qwen/Qwen3.6-35B-A3B-FP8
SPEC='{"method":"mtp","num_speculative_tokens":1}'
RESULT_DIR="${RESULT_DIR:-/tmp/qwen36-ep-exp-$(date -u +%Y%m%d)}"

NAME_COLL=qwen36-exp-ep-colloc
NAME_P=qwen36-exp-ep-p45
NAME_D=qwen36-exp-ep-d67
COLL_PORT=8730
P_PORT=8740
D_PORT=8741
PROXY_PORT=8750
NIXL_P=5740
NIXL_D=5741
PROXY_PID=/tmp/qwen36-exp-ep-proxy.pid
PROXY_LOG=/tmp/qwen36-exp-ep-proxy.log
MOE_SCRIPT="${DIR}/run-pd-exp-moe-gpu47.sh"

UCX_INTRANODE_TLS=sm,self,cuda_ipc,cuda_copy

docker_common() {
  local gpus="$1" cuda="$2" name="$3"
  shift 3
  docker run -d \
    --name "$name" \
    --gpus '"device='"${gpus}"'"' \
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
    -e NVIDIA_VISIBLE_DEVICES="${gpus}" \
    -e PYTHONHASHSEED=0 \
    -e PYTHONUNBUFFERED=1 \
    -e VLLM_USE_DEEP_GEMM=0 \
    -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
    -e CUDA_VISIBLE_DEVICES="${cuda}" \
    -e UCX_TLS="${UCX_INTRANODE_TLS}" \
    -e UCX_MEMTYPE_CACHE=n \
    -e UCX_RNDV_SCHEME=put_zcopy \
    -e UCX_RNDV_THRESH=0 \
    -e UCX_PROTO_ENABLE=y \
    -e UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on \
    -e UCX_CUDA_IPC_BW=50000MBs \
    -e CUDA_DEVICE_MAX_CONNECTIONS=8 \
    "$IMAGE" \
    "$@"
}

model_args() {
  MODEL_ARGS=(
    "${MODEL}"
    --host 127.0.0.1
    --served-model-name "${SERVED}"
    --tensor-parallel-size 1
    --data-parallel-size 2
    --enable-expert-parallel
    --all2all-backend "${ALL2ALL_BACKEND}"
    --kv-cache-dtype fp8
    --enable-prefix-caching
    --mamba-cache-mode align
    --max-model-len 32768
    --gpu-memory-utilization 0.78
    --enable-auto-tool-choice
    --tool-call-parser qwen3_coder
    --async-scheduling
    --reasoning-parser qwen3
    --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
    --default-chat-template-kwargs '{"enable_thinking": false}'
    --speculative-config "${SPEC}"
    --log-error-stack
    --uvicorn-log-level warning
    --max-num-seqs 32
    --max-num-batched-tokens 16384
  )
}

wait_http() {
  local url="$1" n=0
  while (( n < 150 )); do
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

cmd_stop() {
  stop_proxy
  docker rm -f "$NAME_COLL" "$NAME_P" "$NAME_D" 2>/dev/null || true
}

cmd_prepare_47() {
  echo "prepare GPU4+7 (collocated EP / PD baseline aux)"
  docker stop vllm-qwen38-gpu4 vllm-fast-ingest-llm vllm-embed-vl-g7-a vllm-embed 2>/dev/null || true
  sleep 3
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed -n '5,8p'
}

cmd_prepare_4576() {
  echo "prepare GPU4-7 (PD+EP: stop aux on 5-7)"
  docker stop vllm-qwen38-gpu4 vllm-fast-ingest-llm vllm-embed-vl-g7-a vllm-embed vllm-rerank vllm-rerank-vl 2>/dev/null || true
  sleep 5
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader | sed -n '5,9p'
}

cmd_restore_aux() {
  docker start vllm-rerank-vl vllm-rerank vllm-embed vllm-fast-ingest-llm vllm-embed-vl-g7-a vllm-qwen38-gpu4 2>/dev/null || true
}

start_collocated() {
  local backend="${1:-allgather_reducescatter}"
  export ALL2ALL_BACKEND="$backend"
  docker rm -f "$NAME_COLL" 2>/dev/null || true
  model_args
  docker_common "4,7" "0,1" "$NAME_COLL" \
    "${MODEL_ARGS[@]}" \
    --port "${COLL_PORT}"
  echo "started collocated EP backend=${backend} port=${COLL_PORT}"
}

start_pd_ep() {
  local p_backend="${1:-allgather_reducescatter}"
  local d_backend="${2:-allgather_reducescatter}"
  docker rm -f "$NAME_P" "$NAME_D" 2>/dev/null || true
  # D first (GPU 6+7)
  export ALL2ALL_BACKEND="$d_backend"
  model_args
  local kv_d='{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"num_threads":8}}'
  docker run -d \
    --name "$NAME_D" \
    --gpus '"device=6,7"' \
    --network host --ipc host --pid host \
    --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --device /dev/nvidia-caps/nvidia-cap1 --device /dev/nvidia-caps/nvidia-cap2 \
    -v /data/twj/models:/root/.cache/models:ro \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.12/sitecustomize.py:ro" \
    -e NVIDIA_VISIBLE_DEVICES=6,7 -e CUDA_VISIBLE_DEVICES=0,1 \
    -e VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.1 -e VLLM_NIXL_SIDE_CHANNEL_PORT="${NIXL_D}" \
    -e UCX_TLS="${UCX_INTRANODE_TLS}" -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
    "$IMAGE" \
    "${MODEL_ARGS[@]}" \
    --port "${D_PORT}" \
    --kv-transfer-config "${kv_d}"
  wait_http "http://127.0.0.1:${D_PORT}/v1/models" || return 1

  export ALL2ALL_BACKEND="$p_backend"
  model_args
  local kv_p='{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"kv_lease_duration":120,"num_threads":8}}'
  docker run -d \
    --name "$NAME_P" \
    --gpus '"device=4,5"' \
    --network host --ipc host --pid host \
    --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --device /dev/nvidia-caps/nvidia-cap1 --device /dev/nvidia-caps/nvidia-cap2 \
    -v /data/twj/models:/root/.cache/models:ro \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.12/sitecustomize.py:ro" \
    -e NVIDIA_VISIBLE_DEVICES=4,5 -e CUDA_VISIBLE_DEVICES=0,1 \
    -e VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.1 -e VLLM_NIXL_SIDE_CHANNEL_PORT="${NIXL_P}" \
    -e UCX_TLS="${UCX_INTRANODE_TLS}" -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
    "$IMAGE" \
    "${MODEL_ARGS[@]}" \
    --port "${P_PORT}" \
    --kv-transfer-config "${kv_p}" \
    --scheduling-policy fcfs \
    --kv-cache-memory 17179869184
  wait_http "http://127.0.0.1:${P_PORT}/v1/models" || return 1

  stop_proxy
  nohup python3 "${DIR}/pd_pair_proxy.py" --port "${PROXY_PORT}" \
    --prefill "http://127.0.0.1:${P_PORT}" \
    --decode "http://127.0.0.1:${D_PORT}" \
    >"$PROXY_LOG" 2>&1 &
  echo $! >"$PROXY_PID"
  sleep 2
  wait_http "http://127.0.0.1:${PROXY_PORT}/health" || return 1
  echo "started PD+EP P=${p_backend} D=${d_backend} proxy=${PROXY_PORT}"
}

cmd_bench() {
  local port="$1" label="$2"
  mkdir -p "$RESULT_DIR"
  local out="${RESULT_DIR}/bench-${label}.json"
  python3 - <<'PY' "$out" "$port"
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

def one():
    t0 = time.perf_counter()
    req = urllib.request.Request(base, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        json.load(r)
    return time.perf_counter() - t0

one()
lat = []
t0 = time.perf_counter()
for _ in range(12):
    lat.append(one())
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
    "port": port,
}
print(json.dumps(res, indent=2))
open(out, "w").write(json.dumps(res, indent=2))
PY
  echo "wrote $out"
}

try_deepep_backends() {
  # Returns "p_backend d_backend" — use DeepEP if import works in image.
  if docker run --rm --entrypoint python3 "$IMAGE" -c "import deep_ep" 2>/dev/null; then
    echo "deepep_high_throughput deepep_low_latency"
  else
    echo "allgather_reducescatter allgather_reducescatter"
  fi
}

cmd_compare() {
  mkdir -p "$RESULT_DIR"
  local summary="${RESULT_DIR}/ep-compare-summary.txt"
  : >"$summary"
  read -r P_BE D_BE <<<"$(try_deepep_backends)"
  echo "all2all backends: P=${P_BE} D=${D_BE}" | tee -a "$summary"

  # 1) Prior PD no-EP baseline (GPU 4+7)
  echo "----- pd-no-ep (1P+1D TP1) -----" | tee -a "$summary"
  if [[ -x "$MOE_SCRIPT" ]]; then
    if "$MOE_SCRIPT" prepare && "$MOE_SCRIPT" start default && "$MOE_SCRIPT" bench pd-no-ep; then
      cp /tmp/qwen36-moe-exp-"$(date -u +%Y%m%d)"/bench-pd-no-ep.json "${RESULT_DIR}/bench-pd-no-ep.json" 2>/dev/null \
        || cp "$(ls -td /tmp/qwen36-moe-exp-*/bench-pd-no-ep.json 2>/dev/null | head -1)" "${RESULT_DIR}/bench-pd-no-ep.json" 2>/dev/null || true
      "$MOE_SCRIPT" stop
    else
      echo "pd-no-ep: FAIL" | tee -a "$summary"
      "$MOE_SCRIPT" stop 2>/dev/null || true
    fi
  fi
  if [[ -f "${RESULT_DIR}/bench-pd-no-ep.json" ]]; then
    python3 -c "import json; d=json.load(open('${RESULT_DIR}/bench-pd-no-ep.json')); print('pd-no-ep: OK qps=%.3f p50=%.3f'%(d['qps'],d['p50_s']))" | tee -a "$summary"
  fi
  sleep 5

  # 2) Collocated EP2 on 4+7
  echo "----- collocated-ep2 (4+7) -----" | tee -a "$summary"
  cmd_prepare_47
  cmd_stop
  if start_collocated allgather_reducescatter && wait_http "http://127.0.0.1:${COLL_PORT}/v1/models"; then
    cmd_bench "${COLL_PORT}" collocated-ep2 || echo "collocated: BENCH_FAIL" | tee -a "$summary"
    docker logs "$NAME_COLL" 2>&1 | grep -iE 'expert|all2all|deepep|EngineCore' | tail -12 | tee -a "${RESULT_DIR}/collocated-log-snippet.txt" || true
    if [[ -f "${RESULT_DIR}/bench-collocated-ep2.json" ]]; then
      python3 -c "import json; d=json.load(open('${RESULT_DIR}/bench-collocated-ep2.json')); print('collocated-ep2: OK qps=%.3f p50=%.3f'%(d['qps'],d['p50_s']))" | tee -a "$summary"
    fi
  else
    echo "collocated-ep2: START_FAIL" | tee -a "$summary"
    docker logs "$NAME_COLL" 2>&1 | tail -30 | tee -a "$summary" || true
  fi
  cmd_stop
  sleep 5

  # 3) PD + EP2 (4+5 P, 6+7 D)
  echo "----- pd-ep2 (P 4+5, D 6+7) -----" | tee -a "$summary"
  cmd_prepare_4576
  if start_pd_ep "$P_BE" "$D_BE"; then
    cmd_bench "${PROXY_PORT}" pd-ep2 || echo "pd-ep2: BENCH_FAIL" | tee -a "$summary"
    docker logs "$NAME_D" 2>&1 | grep -iE 'expert|all2all|nixl' | tail -8 | tee -a "${RESULT_DIR}/pd-ep-d-log.txt" || true
    if [[ -f "${RESULT_DIR}/bench-pd-ep2.json" ]]; then
      python3 -c "import json; d=json.load(open('${RESULT_DIR}/bench-pd-ep2.json')); print('pd-ep2: OK qps=%.3f p50=%.3f'%(d['qps'],d['p50_s']))" | tee -a "$summary"
    fi
  else
    echo "pd-ep2: START_FAIL" | tee -a "$summary"
    docker logs "$NAME_D" 2>&1 | tail -20 | tee -a "$summary" || true
    docker logs "$NAME_P" 2>&1 | tail -20 | tee -a "$summary" || true
  fi
  cmd_stop
  cmd_restore_aux

  echo "=== summary ===" | tee -a "$summary"
  cat "$summary"
  echo "Results: ${RESULT_DIR}"
}

usage() {
  echo "Usage: $0 {prepare-47|prepare-4576|restore-aux|stop|start-collocated [all2all]|start-pd-ep [p_backend] [d_backend]|bench <port> <label>|compare}"
}

case "${1:-}" in
  prepare-47) cmd_prepare_47 ;;
  prepare-4576) cmd_prepare_4576 ;;
  restore-aux) cmd_restore_aux ;;
  stop) cmd_stop ;;
  start-collocated) start_collocated "${2:-allgather_reducescatter}" ;;
  start-pd-ep) start_pd_ep "${2:-allgather_reducescatter}" "${3:-allgather_reducescatter}" ;;
  bench) cmd_bench "${2:?port}" "${3:-run}" ;;
  compare) cmd_compare ;;
  *) usage; exit 2 ;;
esac
