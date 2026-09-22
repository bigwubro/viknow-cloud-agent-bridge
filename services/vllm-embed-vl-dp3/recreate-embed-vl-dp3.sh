#!/usr/bin/env bash
# Replace vllm-embed-vl + g5 (:8611) + g7-a (:8612) with one DP3 on GPUs 5,6,7 · :8601.
# Does NOT touch vllm-embed (:8502), rerank, whisper, PD, etc.
set -euo pipefail

IMAGE="${VLLM_IMAGE:-vllm-viknow:0.19.0}"
NET="${VLLM_DOCKER_NET:-vllm-server_net}"
NAME="${EMBED_VL_CONTAINER:-vllm-embed-vl}"
PORT="${EMBED_VL_PORT:-8601}"
# Same three GPUs as the old three single-GPU instances
GPUS="${EMBED_VL_DP_GPUS:-5,6,7}"
DP_SIZE="${EMBED_VL_DP_SIZE:-3}"

OLD=(vllm-embed-vl vllm-embed-vl-g5 vllm-embed-vl-g7-a)

if [[ -z "${EMBED_VL_API_KEY:-}" ]]; then
  if docker inspect vllm-embed-vl &>/dev/null; then
    EMBED_VL_API_KEY="$(docker inspect vllm-embed-vl --format '{{range .Config.Cmd}}{{println .}}{{end}}' \
      | sed -n 's/.*--api-key[[:space:]]*//p' | head -1)"
  fi
fi
if [[ -z "${EMBED_VL_API_KEY:-}" ]]; then
  echo "Set EMBED_VL_API_KEY or keep one of ${OLD[*]} running to copy the key from." >&2
  exit 1
fi

if docker inspect vllm-embed &>/dev/null; then
  embed_gpu="$(docker inspect vllm-embed --format '{{json .HostConfig.DeviceRequests}}' \
    | python3 -c "import sys,json; r=json.load(sys.stdin); print((r[0].get('DeviceIDs') or ['?'])[0])" 2>/dev/null || echo '?')"
  if [[ ",${GPUS}," == *",${embed_gpu},"* ]]; then
    echo "WARN: vllm-embed (8502) is still on GPU ${embed_gpu}, which is in DP GPUs=${GPUS}." >&2
    echo "      DP3 and vllm-embed may OOM or contend. Move vllm-embed to another GPU first," >&2
    echo "      or set ALLOW_GPU7_SHARE=1 to continue anyway." >&2
    if [[ "${ALLOW_GPU7_SHARE:-}" != "1" ]]; then
      exit 2
    fi
  fi
fi

for c in "${OLD[@]}"; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    echo "Removing $c"
    docker rm -f "$c"
  fi
done

# Drop stale name if we are renaming
if [[ "$NAME" != "vllm-embed-vl" ]] && docker ps -a --format '{{.Names}}' | grep -qx vllm-embed-vl; then
  docker rm -f vllm-embed-vl 2>/dev/null || true
fi

read -r -d '' SERVE_CMD <<EOF || true
vllm serve /root/.cache/models/Qwen/Qwen3-VL-Embedding-2B \\
  --served-model-name Qwen/Qwen3-VL-Embedding-2B \\
  --api-key ${EMBED_VL_API_KEY} \\
  --host 0.0.0.0 \\
  --port ${PORT} \\
  --gpu-memory-utilization 0.32 \\
  --data-parallel-size ${DP_SIZE} \\
  --api-server-count 1 \\
  --max-model-len 16384 \\
  --runner pooling \\
  --hf_overrides '{"is_matryoshka": true, "matryoshka_dimensions": [64,128,256,512,1024,2048]}' \\
  --pooler-config '{"dimensions": 1024}'
EOF

echo "Starting ${NAME} DP${DP_SIZE} on GPUs ${GPUS} :${PORT}"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d \
  --name "$NAME" \
  --gpus "\"device=${GPUS}\"" \
  --network "$NET" \
  --ipc host \
  --shm-size 67108864 \
  --restart unless-stopped \
  -p "0.0.0.0:${PORT}:${PORT}" \
  -v /data/twj/models:/root/.cache/models:rw \
  --entrypoint /bin/bash \
  "$IMAGE" \
  -c "$SERVE_CMD"

sleep 5
curl -sf -H "Authorization: Bearer ${EMBED_VL_API_KEY}" "http://127.0.0.1:${PORT}/v1/models" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('models:', [m['id'] for m in d.get('data', [])])
"
echo "Done. :8611 / :8612 are free; point clients at :${PORT} only."
