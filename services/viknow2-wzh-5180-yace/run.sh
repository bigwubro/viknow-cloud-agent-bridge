#!/bin/bash
# Restart viknow2-wzh-5180-yace (same image/data as 5175, models direct to 236).
set -euo pipefail
DIR=/home/cursor-agent/services/viknow2-wzh-5180-yace
NAME=viknow2-wzh-5180-yace
IMAGE=viknow2-app:d5079b59411b

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --privileged \
  --network host \
  --cgroupns host \
  --add-host host.docker.internal:127.0.0.1 \
  --add-host ai.qingxiang.tech:172.30.57.94 \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=unconfined \
  --security-opt label=disable \
  --env-file "$DIR/container.env" \
  -v /data:/data:rw \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v viknow2_wzh_5180_yace_state:/opt/viknow/.state \
  -v "$DIR/app.yaml:/opt/viknow/config/test/app.yaml:ro" \
  -v "$DIR/patches/model_gateway.py:/usr/local/lib/python3.12/site-packages/viknow/core/model_gateway.py:ro" \
  -v "$DIR/patches/multimodal.yaml:/opt/viknow/config/test/tools/multimodal.yaml:ro" \
  "$IMAGE" \
  sh -c 'exec uvicorn viknow.api.app:create_app --factory --host 0.0.0.0 --port ${PORT}'

echo "started $NAME on :5180"
echo "default model: private:Qwen/Qwen3.6-35B-A3B-FP8 -> 127.0.0.1:8500"
echo "embed 127.0.0.1:8601  rerank 127.0.0.1:8602  asr 127.0.0.1:8503"
