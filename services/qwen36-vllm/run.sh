#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=vllm-vlm
PORT=8500
MODEL=/root/.cache/models/Qwen/Qwen3___6-35B-A3B-FP8
IMAGE="${VLLM_IMAGE:-vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688}"

# 44688-aligned serving:
#   1) --api-server-count 4
#   2) DP4 x TP1 (request-level replicas; model fits one 96GB card)
#   3) --renderer-num-workers 4 + SHM cache (needs the r44688 image patch)
# Full upstream PRs #44786/#44787 are NOT in this image.

docker rm -f "$NAME" 2>/dev/null || true
docker run -d \
  --name "$NAME" \
  --gpus '"device=0,1,2,3"' \
  --network vllm-server_net \
  --ipc host \
  -p 127.0.0.1:${PORT}:${PORT} \
  -v /data/twj/models:/root/.cache/models:ro \
  -e VLLM_OBJECT_STORAGE_SHM_BUFFER_NAME=vllm_mm_cache_8500_bt16384 \
  -e PYTHONHASHSEED=0 \
  -e VLLM_USE_DEEP_GEMM=0 \
  -e VLLM_ENGINE_READY_TIMEOUT_S=1800 \
  "$IMAGE" \
  "${MODEL}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --served-model-name Qwen/Qwen3.6-35B-A3B-FP8 \
  --tensor-parallel-size 1 \
  --data-parallel-size 4 \
  --api-server-count 4 \
  --kv-cache-dtype fp8 \
  --enable-prefix-caching \
  --mamba-cache-mode align \
  --max-model-len 156000 \
  --max-num-seqs 64 \
  --max-num-batched-tokens 16384 \
  --max-num-partial-prefills 1 \
  --max-long-partial-prefills 1 \
  --long-prefill-token-threshold 16384 \
  --gpu-memory-utilization 0.75 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":2}' \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --async-scheduling \
  --scheduling-policy priority \
  --reasoning-parser qwen3 \
  --reasoning-config '{"reasoning_start_str":"<think>","reasoning_end_str":"</think>"}' \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  --renderer-num-workers 4 \
  --mm-encoder-tp-mode data \
  --mm-processor-cache-type shm \
  --mm-processor-cache-gb 48 \
  --mm-shm-cache-max-object-size-mb 512 \
  --log-error-stack \
  --uvicorn-log-level debug \
  --enable-log-requests \
  --enable-logging-iteration-details

echo "Started ${NAME} (dp4+tp1, api-servers=4, renderer-workers=4, image=${IMAGE})"
