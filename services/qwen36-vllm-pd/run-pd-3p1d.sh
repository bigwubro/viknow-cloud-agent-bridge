#!/usr/bin/env bash
# One 3P+1D group behind 127.0.0.1:8500. Does not touch viknow2-test.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Official 0.29.0. Custom vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688 is left on disk.
IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.29.0}"
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
SERVED=Qwen/Qwen3.6-35B-A3B-FP8
# P and D must share this so NIXL page size matches.
SPEC='{"method":"mtp","num_speculative_tokens":1}'
# Unified attention block logged as 2112. 2N-1 snapshots every GDN block.
LMC_CHUNK=2112
P_BATCHED=4223
ENABLE_LMCACHE_P2P="${ENABLE_LMCACHE_P2P:-1}"
LMC_COORD_PORT=9301
LMC_COORD_URL="http://127.0.0.1:${LMC_COORD_PORT}"

# GPU0/1/2 prefill :8510/:8511/:8512, GPU3 decode :8513, proxy :8520, nginx :8500
# LMCache ZMQ 6560-6563, HTTP 8060-8063, P2P 7160-7162, coordinator 9301.

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
  --speculative-config "${SPEC}"
  --log-error-stack
  --uvicorn-log-level warning
)

# Same UCX / NIXL pull path as run-pd-1p1d.sh. All four GPUs stay visible so
# each P can CUDA-IPC to D, and D can GET from any of the three P cards.
UCX_INTRANODE_TLS=sm,self,cuda_ipc,cuda_copy
ALL_GPUS=0,1,2,3

kv_config() {
  local role="$1" lmc_port="$2"
  python3 - "$role" "$lmc_port" <<'PY'
import json, sys
role, port = sys.argv[1], int(sys.argv[2])
nixl_role = "kv_producer" if role == "p" else "kv_consumer"
nixl_extra = {"num_threads": 8}
if role == "p":
    nixl_extra["kv_lease_duration"] = 120
print(json.dumps({
    "kv_connector": "MultiConnector",
    "kv_role": nixl_role,
    "kv_connector_extra_config": {
        "connectors": [
            {
                "kv_connector": "NixlConnector",
                "kv_role": nixl_role,
                "kv_load_failure_policy": "fail",
                "kv_connector_extra_config": nixl_extra,
            },
            {
                "kv_connector": "LMCacheMPConnector",
                "kv_role": "kv_both",
                "kv_connector_extra_config": {
                    "lmcache.mp.host": "tcp://127.0.0.1",
                    "lmcache.mp.port": port,
                },
            },
        ]
    },
}, separators=(",", ":")))
PY
}

start_coordinator() {
  docker rm -f qwen36-lmc-coord 2>/dev/null || true
  if [[ "${ENABLE_LMCACHE_P2P}" != "1" ]]; then
    echo "LMCache P2P off; no coordinator"
    return 0
  fi
  docker run -d \
    --name qwen36-lmc-coord \
    --network host \
    --entrypoint lmcache \
    -e LMCACHE_DISABLE_BANNER=1 \
    -e PYTHONUNBUFFERED=1 \
    "$IMAGE" \
    coordinator \
    --host 127.0.0.1 \
    --port "${LMC_COORD_PORT}" \
    --chunk-size "${LMC_CHUNK}" \
    --disable-metrics
  echo "started qwen36-lmc-coord :${LMC_COORD_PORT}"
}

start_engine() {
  local name="$1" cuda_visible="$2" port="$3" role="$4" nixl_port="$5"
  local lmc_zmq="$6" lmc_http="$7" lmc_id="$8" lmc_l1="$9"
  local p2p_url="${10:-}"
  local kv extra=()
  kv="$(kv_config "$role" "$lmc_zmq")"
  if [[ "$role" == p ]]; then
    extra+=(--scheduling-policy fcfs --max-num-seqs 64 --max-num-batched-tokens "${P_BATCHED}")
  else
    extra+=(--max-num-seqs 64 --max-num-batched-tokens 16384)
  fi
  docker rm -f "$name" 2>/dev/null || true
  # CUDA_VISIBLE_DEVICES lists the compute GPU first (TP1 uses device 0).
  # Remaining IDs keep the peer cards visible for UCX cuda_ipc.
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
    --entrypoint /entry-pd-lmcache.sh \
    -v /data/twj/models:/root/.cache/models:ro \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.12/sitecustomize.py:ro" \
    -v "${DIR}/sitecustomize.py:/usr/lib/python3.13/sitecustomize.py:ro" \
    -v "${DIR}/entry-pd-lmcache.sh:/entry-pd-lmcache.sh:ro" \
    -e NVIDIA_VISIBLE_DEVICES="${ALL_GPUS}" \
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
    -e UCX_PROTO_INFO=y \
    -e UCX_LOG_LEVEL=info \
    -e UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on \
    -e UCX_CUDA_IPC_BW=50000MBs \
    -e CUDA_DEVICE_MAX_CONNECTIONS=8 \
    -e LMC_ZMQ_PORT="${lmc_zmq}" \
    -e LMC_HTTP_PORT="${lmc_http}" \
    -e LMC_INSTANCE_ID="${lmc_id}" \
    -e LMC_L1_GB="${lmc_l1}" \
    -e LMC_CHUNK="${LMC_CHUNK}" \
    -e LMC_COORD_URL="$([[ -n "${p2p_url}" ]] && echo "${LMC_COORD_URL}" || true)" \
    -e LMC_P2P_URL="${p2p_url}" \
    "$IMAGE" \
    "${COMMON[@]}" \
    --port "${port}" \
    --kv-transfer-config "${kv}" \
    "${extra[@]}"
  echo "started ${name} visible=${cuda_visible} port=${port} role=${role} nixl=${nixl_port} lmc=${lmc_zmq}/${lmc_http} p2p=${p2p_url:-off}"
}

echo "Stopping unified DP4 container vllm-vlm to free GPU 0-3 and :8500"
docker stop vllm-vlm 2>/dev/null || true

# Drop the old 2x1P+1D decode on GPU1 so that card can become P.
docker rm -f qwen36-d1 2>/dev/null || true

start_coordinator

# Compute GPU first, then D (for P) or all P cards (for D).
# Three P servers join P2P so the 16k shared prefix can move across cards.
if [[ "${ENABLE_LMCACHE_P2P}" == "1" ]]; then
  start_engine qwen36-p0 0,3,1,2 8510 p 5600 6560 8060 qwen36-p0 16 127.0.0.1:7160
  start_engine qwen36-p1 1,3,0,2 8511 p 5601 6561 8061 qwen36-p1 16 127.0.0.1:7161
  start_engine qwen36-p2 2,3,0,1 8512 p 5602 6562 8062 qwen36-p2 16 127.0.0.1:7162
else
  start_engine qwen36-p0 0,3,1,2 8510 p 5600 6560 8060 qwen36-p0 16
  start_engine qwen36-p1 1,3,0,2 8511 p 5601 6561 8061 qwen36-p1 16
  start_engine qwen36-p2 2,3,0,1 8512 p 5602 6562 8062 qwen36-p2 16
fi
start_engine qwen36-d3 3,0,1,2 8513 d 5603 6563 8063 qwen36-d3 8

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
stop_pidfile /tmp/qwen36-pd-proxy-3p1d.pid
nohup python3 "${DIR}/pd_pair_proxy.py" --port 8520 \
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

echo "PD 3P+1D + LMCache is on 127.0.0.1:8500"
curl -sS http://127.0.0.1:8500/v1/models | head -c 400; echo
curl -sS http://127.0.0.1:8520/health; echo
for p in 8060 8061 8062 8063; do
  echo "lmcache :${p}"; curl -sS -m 2 "http://127.0.0.1:${p}/status" | head -c 300; echo
done
