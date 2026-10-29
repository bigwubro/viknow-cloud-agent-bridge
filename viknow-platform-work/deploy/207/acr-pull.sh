#!/usr/bin/env bash
set -euo pipefail
ACR_USER="${ACR_USER:-zihan.wu@litesense}"
ACR_PASSWORD="${ACR_PASSWORD:?set ACR_PASSWORD for docker login (do not commit)}"
REG=registry.cn-hangzhou.aliyuncs.com

echo "$ACR_PASSWORD" | docker login "$REG" -u "$ACR_USER" --password-stdin

pull=(
  litesense/viknow2-pgvector-textsearch:pg18-trixie-v1.3.1
  litesense/viknow2-redis:7
  litesense/viknow2-neo4j:5.26.3.0
  litesense/viknow2-minio:latest
)
for img in "${pull[@]}"; do
  docker pull "${REG}/${img}"
done
echo "ACR pull OK"
