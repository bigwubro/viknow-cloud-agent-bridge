#!/usr/bin/env bash
# Build Playground image on 236 and push to Aliyun ACR (207 pulls VIKNOW_PLATFORM_IMAGE).
set -euo pipefail

ROOT="${VIKNOW_PLATFORM_ROOT:-/home/cursor-agent/src/viknow-platform}"
REG=registry.cn-hangzhou.aliyuncs.com
ACR_USER="${ACR_USER:-zihan.wu@litesense}"
ACR_PASSWORD="${ACR_PASSWORD:?set ACR_PASSWORD}"

cd "$ROOT"
SHA="$(git rev-parse --short=12 HEAD 2>/dev/null || echo manual)"
IMAGE="${REG}/litesense/viknow-platform-playground:207-${SHA}"
LATEST="${REG}/litesense/viknow-platform-playground:207-latest"

echo "Building ${IMAGE} ..."
docker build -f deploy/236/Dockerfile -t "$IMAGE" -t "$LATEST" .

echo "$ACR_PASSWORD" | docker login "$REG" -u "$ACR_USER" --password-stdin
docker push "$IMAGE"
docker push "$LATEST"
docker logout "$REG" 2>/dev/null || true

echo "Pushed:"
echo "  $IMAGE"
echo "  $LATEST"
echo "On 207 set in ~/viknow207.host.env:"
echo "  VIKNOW_PLATFORM_IMAGE=$LATEST"
