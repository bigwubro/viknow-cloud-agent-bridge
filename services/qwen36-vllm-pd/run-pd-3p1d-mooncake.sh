#!/usr/bin/env bash
# 3P+1D + Mooncake Store (cross-P prefix pool). Nixl still does P→D.
# Does not touch viknow2-test. Rollback: ./run-pd-3p1d.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.29.0}"
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
SERVED=Qwen/Qwen3.6-35B-A3B-FP8
SPEC='{"method":"mtp","num_speculative_tokens":1}'
UCX_INTRANODE_TLS=sm,self,cuda_ipc,cuda_copy
ALL_GPUS=0,1,2,3

COMMON=(
  "${MODEL}"
  --host 127.0.0.1
  --served-model-name "${SERVED}"
  --tensor-parallel-size 1
  --kv-cache-dtype fp8
  --enable-prefix-caching
  --mamba-cache-mode align
  --max-model-len 156000
  --enable-auto-tool-choice
  --tool-call-parser qwen3_coder
  --async-scheduling
  --reasoning-parser qwen3
  --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}'
  --default-chat-template-kwargs '{"enable_thinking": false}'
  --speculative-config "${SPEC}"
  --log-error-stack
  --uvicorn-log-level warning
)

# Nixl first so this request still P→D over CUDA IPC. Store second is the
# shared prefix pool. cache_prefix keeps this tenant off any other master user.
P_KV='{"kv_connector":"MultiConnector","kv_role":"kv_producer","kv_connector_extra_config":{"connectors":[{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"kv_lease_duration":120,"num_threads":8}},{"kv_connector":"MooncakeStoreConnector","kv_role":"kv_both","kv_connector_extra_config":{"load_async":true,"lookup_async":true,"cache_prefix":"qwen36-3p1d"}}]}}'
# D stays Nixl-only. MooncakeStoreConnector on a PD consumer asserts
# Missing current block table in build_connector_meta (0.29 scheduler.py:424).
D_KV='{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail","kv_connector_extra_config":{"num_threads":8}}'

start_master() {
  docker rm -f qwen36-mooncake-master 2>/dev/null || true
  docker run -d \
    --name qwen36-mooncake-master \
    --network host \
    --entrypoint mooncake_master \
    "$IMAGE" \
    --rpc_port=50051 \
    --port=50051 \
    --default_kv_lease_ttl=1800000 \
    --default_kv_soft_pin_ttl=1800000 \
    --client_ttl=1800
  echo "started qwen36-mooncake-master :50051"
}

start_engine() {
  local name="$1" cuda_visible="$2" port="$3" role="$4" nixl_port="$5"
  local kv extra=()
  if [[ "$role" == p ]]; then
    kv="$P_KV"
    # Prefill-only: raise the step token budget so ~20k unique tails pack
    # together. 0.90 keeps KV from shrinking when activation grows.
    extra+=(--gpu-memory-utilization 0.84 --scheduling-policy fcfs --max-num-seqs 256 --max-num-batched-tokens 131072)
  else
    kv="$D_KV"
    extra+=(--gpu-memory-utilization 0.75 --max-num-seqs 64 --max-num-batched-tokens 16384)
  fi
  docker rm -f "$name" 2>/dev/null || true
  docker run -d \
    --name "$name" \
    --gpus '"device='"${ALL_GPUS}"'"' \
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
    -v "${DIR}/mooncake_config.json:/etc/mooncake_config.json:ro" \
    -e NVIDIA_VISIBLE_DEVICES="${ALL_GPUS}" \
    -e PYTHONHASHSEED=0 \
    -e PYTHONUNBUFFERED=1 \
    -e VLLM_USE_DEEP_GEMM=0 \
    -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
    -e VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.1 \
    -e VLLM_NIXL_SIDE_CHANNEL_PORT="${nixl_port}" \
    -e VLLM_SSM_CONV_STATE_LAYOUT=DS \
    -e MOONCAKE_CONFIG_PATH=/etc/mooncake_config.json \
    -e MOONCAKE_PROTOCOL=tcp \
    -e CUDA_VISIBLE_DEVICES="${cuda_visible}" \
    -e UCX_TLS="${UCX_INTRANODE_TLS}" \
    -e UCX_MEMTYPE_CACHE=n \
    -e UCX_RNDV_SCHEME=put_zcopy \
    -e UCX_RNDV_THRESH=0 \
    -e UCX_PROTO_ENABLE=y \
    -e UCX_PROTO_INFO=y \
    -e UCX_LOG_LEVEL=info \
    -e UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on \
    -e UCX_CUDA_IPC_BW=50000MBs \
    -e CUDA_DEVICE_MAX_CONNECTIONS=8 \
    "$IMAGE" \
    "${COMMON[@]}" \
    --port "${port}" \
    --kv-transfer-config "${kv}" \
    "${extra[@]}"
  echo "started ${name} visible=${cuda_visible} port=${port} role=${role} nixl=${nixl_port} mooncake"
}

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

restart_one_p() {
  local which="$1"
  case "$which" in
    p0) start_engine qwen36-p0 0,3,1,2 8510 p 5600; wait_http http://127.0.0.1:8510/v1/models ;;
    p1) start_engine qwen36-p1 1,3,0,2 8511 p 5601; wait_http http://127.0.0.1:8511/v1/models ;;
    p2) start_engine qwen36-p2 2,3,0,1 8512 p 5602; wait_http http://127.0.0.1:8512/v1/models ;;
    *) echo "usage: $0 restart-p p0|p1|p2" >&2; return 2 ;;
  esac
}

if [[ "${1:-}" == "restart-p" ]]; then
  restart_one_p "${2:-}"
  exit $?
fi

if [[ "${1:-}" == "restart-pd" ]]; then
  # Bounce P+D only. Master / proxy / nginx stay. Needed because a single
  # P restart sees ~48 GiB free (peer CUDA contexts) and cannot raise util.
  start_engine qwen36-p0 0,3,1,2 8510 p 5600
  start_engine qwen36-p1 1,3,0,2 8511 p 5601
  start_engine qwen36-p2 2,3,0,1 8512 p 5602
  start_engine qwen36-d3 3,0,1,2 8513 d 5603
  wait_http http://127.0.0.1:8510/v1/models
  wait_http http://127.0.0.1:8511/v1/models
  wait_http http://127.0.0.1:8512/v1/models
  wait_http http://127.0.0.1:8513/v1/models
  echo "restart-pd ready"
  exit 0
fi

echo "Stopping unified DP4 container vllm-vlm to free GPU 0-3 and :8500"
docker stop vllm-vlm 2>/dev/null || true
docker rm -f qwen36-d1 qwen36-lmc-coord qwen36-lmc-p0 qwen36-lmc-p1 qwen36-lmc-p2 qwen36-lmc-d3 2>/dev/null || true

start_master
sleep 2

start_engine qwen36-p0 0,3,1,2 8510 p 5600
start_engine qwen36-p1 1,3,0,2 8511 p 5601
start_engine qwen36-p2 2,3,0,1 8512 p 5602
start_engine qwen36-d3 3,0,1,2 8513 d 5603

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
stop_pidfile /tmp/qwen36-pd-proxy-3p1d.pid
nohup python3 "${DIR}/pd_pair_proxy.py" --port 8520 \
  --route least_inflight \
  --prefill http://127.0.0.1:8510 \
  --prefill http://127.0.0.1:8511 \
  --prefill http://127.0.0.1:8512 \
  --decode http://127.0.0.1:8513 \
  > /tmp/qwen36-pd-proxy-3p1d.log 2>&1 &
echo $! > /tmp/qwen36-pd-proxy-3p1d.pid
sleep 2
wait_http http://127.0.0.1:8520/health

docker rm -f qwen36-pd-lb 2>/dev/null || true
docker run -d --name qwen36-pd-lb --network host \
  -v "${DIR}/nginx-8500-3p1d.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.27-alpine
sleep 1
wait_http http://127.0.0.1:8500/health

echo "PD 3P+1D + Mooncake Store is on 127.0.0.1:8500"
curl -sS http://127.0.0.1:8500/v1/models | head -c 400; echo
curl -sS http://127.0.0.1:8520/health; echo
