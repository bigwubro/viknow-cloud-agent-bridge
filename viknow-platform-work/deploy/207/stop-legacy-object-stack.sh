#!/usr/bin/env bash
# Retire standalone MinIO / JuiceFS gateway stacks on 207 (replaced by viknow-storage).
set -euo pipefail
DEPLOY207="$(cd "$(dirname "$0")" && pwd)"
DOCKER=("$DEPLOY207/docker.sh")

legacy=(
  viknow-juicefs-s3-gateway
  viknow-test-data-minio-1
)
for c in "${legacy[@]}"; do
  if "${DOCKER[@]}" ps -a --format '{{.Names}}' | grep -qx "$c"; then
    echo "Removing legacy container $c"
    "${DOCKER[@]}" rm -f "$c" || true
  fi
done
