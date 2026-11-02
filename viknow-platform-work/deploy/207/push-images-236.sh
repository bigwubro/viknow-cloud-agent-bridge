#!/usr/bin/env bash
# On 236: build (or retag) platform + storage, push to ACR for 207 pull-only deploy.
set -euo pipefail

REG=registry.cn-hangzhou.aliyuncs.com
ACR_USER="${ACR_USER:-zihan.wu@litesense}"
ACR_PASSWORD="${ACR_PASSWORD:?set ACR_PASSWORD}"

PLATFORM_ROOT="${VIKNOW_PLATFORM_ROOT:-/home/cursor-agent/src/viknow-platform}"
STORAGE_ROOT="${VIKNOW_STORAGE_ROOT:-/home/cursor-agent/src/viknow-storage}"

PLATFORM_LATEST="${REG}/litesense/viknow-platform-playground:207-latest"
STORAGE_LATEST="${REG}/litesense/viknow-storage:207-latest"

cd "$PLATFORM_ROOT"
SHA="$(git rev-parse --short=12 HEAD 2>/dev/null || echo manual)"
PLATFORM_TAG="${REG}/litesense/viknow-platform-playground:207-${SHA}"

echo "== Build platform ${PLATFORM_TAG} =="
docker build -f deploy/236/Dockerfile -t "$PLATFORM_TAG" -t "$PLATFORM_LATEST" .

echo "== Build storage ${STORAGE_LATEST} =="
docker build -t "$STORAGE_LATEST" "$STORAGE_ROOT"

echo "$ACR_PASSWORD" | docker login "$REG" -u "$ACR_USER" --password-stdin
docker push "$PLATFORM_TAG"
docker push "$PLATFORM_LATEST"
docker push "$STORAGE_LATEST"
docker logout "$REG" 2>/dev/null || true

echo ""
echo "207 ~/viknow207.host.env:"
echo "VIKNOW_PLATFORM_IMAGE=${PLATFORM_LATEST}"
echo "VIKNOW_STORAGE_IMAGE=${STORAGE_LATEST}"
