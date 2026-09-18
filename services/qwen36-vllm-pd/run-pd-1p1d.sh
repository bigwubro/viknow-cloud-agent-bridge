#!/usr/bin/env bash
# Two 1P+1D pairs behind 127.0.0.1:8500. Does not touch viknow2-test.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${VLLM_IMAGE:-vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688}"
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
SERVED=Qwen/Qwen3.6-35B-A3B-FP8

# pair A: GPU0 prefill :8510, GPU1 decode :8511, proxy :8520
# pair B: GPU2 prefill :8512, GPU3 decode :8513, proxy :8521
# nginx :8500

COMMON=(
  "${MODEL}"
  --host 127.0.0.1
  --served-model-name "${SERVED}"
  --tensor-parallel-size 1
  --kv-cache-dtype fp8
  --enable-prefix-caching
  --mamba-cache-mode align
  --max-model-len 156000
  --gpu-memory-utilization 0.75
  --enable-auto-tool-choice
  --tool-call-parser qwen3_coder
  --async-scheduling
  --reasoning-parser qwen3
  --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
  --default-chat-template-kwargs '{"enable_thinking": false}'
  --log-error-stack
  --uvicorn-log-level warning
)

# Intra-node P2P is OK (nvidia-smi topo -p2p r). Do not use UCX_TLS=all /
# UCX_NET_DEVICES=all: that path measured ~330 MB/s and split across every
# in-flight NIXL READ, so D sat in WAITING_FOR_REMOTE_KVS for 12-40s.
# tcp+sm are required for UCX active messages (NIXL intra-agent setup).
# gdr_copy is not built in this image. Data plane should still pick cuda_ipc.
UCX_INTRANODE_TLS=tcp,sm,cuda_ipc,cuda_copy,self

start_engine() {
  local name="$1" gpu_devices="$2" cuda_visible="$3" port="$4" role="$5" nixl_port="$6"
  local kv extra=()
  if [[ "$role" == p ]]; then
    kv='{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"kv_lease_duration":120,"num_threads":8}}'
    extra+=(--scheduling-policy priority --max-num-seqs 64 --max-num-batched-tokens 16384 --max-num-partial-prefills 1)
  else
    # Decode: FCFS so finished KV pulls enter the batch together. Keep
    # async-scheduling so later pulls overlap with decode. num_threads
    # raises UCX progress threads for concurrent READs.
    kv='{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"num_threads":8}}'
    extra+=(--max-num-seqs 64 --max-num-batched-tokens 16384)
  fi
  docker rm -f "$name" 2>/dev/null || true
  # Pair GPUs must both be visible: --gpus device=N alone hides the peer,
  # so UCX cannot open CUDA IPC and falls back to ~330MB/s host+tcp.
  # CUDA_VISIBLE_DEVICES lists the compute GPU first (TP1 uses device 0).
  docker run -d \
    --name "$name" \
    --gpus '"device='"${gpu_devices}"'"' \
    --network host \
    --ipc host \
    --pid host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    -v /data/twj/models:/root/.cache/models:ro \
    -e NVIDIA_VISIBLE_DEVICES="${gpu_devices}" \
    -e PYTHONHASHSEED=0 \
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
    -e CUDA_DEVICE_MAX_CONNECTIONS=8 \
    "$IMAGE" \
    "${COMMON[@]}" \
    --port "${port}" \
    --kv-transfer-config "${kv}" \
    "${extra[@]}"
  echo "started ${name} gpus=${gpu_devices} visible=${cuda_visible} port=${port} role=${role}"
}

echo "Stopping unified DP4 container vllm-vlm to free GPU 0-3 and :8500"
docker stop vllm-vlm 2>/dev/null || true

# Pair A: physical 0/1 visible to both; P computes on 0, D on 1.
# Pair B: physical 2/3 visible to both; P computes on 2, D on 3.
start_engine qwen36-p0 0,1 0,1 8510 p 5600
start_engine qwen36-d1 0,1 1,0 8511 d 5601
start_engine qwen36-p2 2,3 0,1 8512 p 5602
start_engine qwen36-d3 2,3 1,0 8513 d 5603

wait_http() {
  local url="$1" n=0
  echo "wait ${url}"
  while (( n < 180 )); do
    if curl -sf -m 3 "$url" >/dev/null; then
      echo "ready ${url}"
      return 0
    fi
    sleep 5
    n=$((n + 1))
  done
  echo "timeout ${url}"
  return 1
}

wait_http http://127.0.0.1:8510/v1/models
wait_http http://127.0.0.1:8511/v1/models
wait_http http://127.0.0.1:8512/v1/models
wait_http http://127.0.0.1:8513/v1/models

stop_pidfile() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local pid
    pid="$(cat "$f" || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      sleep 1
    fi
    rm -f "$f"
  fi
}
stop_pidfile /tmp/qwen36-pd-proxy-a.pid
stop_pidfile /tmp/qwen36-pd-proxy-b.pid
nohup python3 "${DIR}/pd_pair_proxy.py" --port 8520 --prefill http://127.0.0.1:8510 --decode http://127.0.0.1:8511 \
  > /tmp/qwen36-pd-proxy-a.log 2>&1 &
echo $! > /tmp/qwen36-pd-proxy-a.pid
nohup python3 "${DIR}/pd_pair_proxy.py" --port 8521 --prefill http://127.0.0.1:8512 --decode http://127.0.0.1:8513 \
  > /tmp/qwen36-pd-proxy-b.log 2>&1 &
echo $! > /tmp/qwen36-pd-proxy-b.pid
sleep 2
wait_http http://127.0.0.1:8520/health
wait_http http://127.0.0.1:8521/health

docker rm -f qwen36-pd-lb 2>/dev/null || true
docker run -d --name qwen36-pd-lb --network host \
  -v "${DIR}/nginx-8500.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.27-alpine
sleep 1
wait_http http://127.0.0.1:8500/health

echo "PD 1P+1D x2 is on 127.0.0.1:8500"
curl -sS http://127.0.0.1:8500/v1/models | head -c 400; echo
