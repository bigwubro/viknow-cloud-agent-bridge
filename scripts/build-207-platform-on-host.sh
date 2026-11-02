#!/usr/bin/env bash
# Build platform + hyperrag worker tags on 207 from synced sources (Tailscale SSH).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLATFORM_ROOT="${VIKNOW_PLATFORM_ROOT:-$ROOT/viknow-platform-work}"
TAG="${VIKNOW207_IMAGE_TAG:-207-latest}"
REG="${ACR_REGISTRY:-registry.cn-hangzhou.aliyuncs.com/litesense}"
PLATFORM_IMAGE="${REG}/viknow-platform-playground:${TAG}"
HYPERRAG_IMAGE="${REG}/viknow2-hyperrag-test:${TAG}"

tar -C "$PLATFORM_ROOT" -czf /tmp/viknow207-platform-build.tgz \
  deploy/236/Dockerfile deploy/236/docker-entrypoint.sh \
  pyproject.toml README.md requirements-hyperrag.txt config src ui scripts/mineru-docker-wrapper \
  external/HyperRAG external/deepagents-sandbox external/actio external/schedule-core

bash "$ROOT/scripts/ssh-207.sh" "TAG='${TAG}' REG='${REG}' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p ~/src/viknow-platform-build
cd ~/src/viknow-platform-build
REMOTE

bash "$ROOT/scripts/ssh-207.sh" 'cat > ~/src/viknow-platform-build.tgz' < /tmp/viknow207-platform-build.tgz

bash "$ROOT/scripts/ssh-207.sh" 'bash -s' <<REMOTE
set -euo pipefail
cd ~/src/viknow-platform-build
tar -xzf ../viknow-platform-build.tgz
sudo docker build -f deploy/236/Dockerfile -t ${PLATFORM_IMAGE} -t ${HYPERRAG_IMAGE} .
echo built ${PLATFORM_IMAGE}
REMOTE
