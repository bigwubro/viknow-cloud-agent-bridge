#!/usr/bin/env bash
set -euo pipefail
ACR_USER="${ACR_USER:-zihan.wu@litesense}"
ACR_PASSWORD="${ACR_PASSWORD:?set ACR_PASSWORD for docker login (do not commit)}"
REG=registry.cn-hangzhou.aliyuncs.com

DEPLOY207="$(cd "$(dirname "$0")" && pwd)"
DOCKER=("$DEPLOY207/docker.sh")

echo "$ACR_PASSWORD" | "${DOCKER[@]}" login "$REG" -u "$ACR_USER" --password-stdin

pull=(
  litesense/viknow2-pgvector-textsearch:pg18-trixie-v1.3.1
  litesense/viknow2-redis:7
  litesense/viknow2-neo4j:5.26.3.0
  litesense/viknow2-minio:latest
)
for var in VIKNOW_PLATFORM_IMAGE VIKNOW_STORAGE_IMAGE; do
  ref="${!var:-}"
  if [[ -n "$ref" && "$ref" == "$REG/"* ]]; then
    pull+=("${ref#${REG}/}")
  elif [[ -n "$ref" ]]; then
    pull+=("$ref")
  fi
done
for img in "${pull[@]}"; do
  "${DOCKER[@]}" pull "${REG}/${img}"
done
echo "ACR pull OK"
