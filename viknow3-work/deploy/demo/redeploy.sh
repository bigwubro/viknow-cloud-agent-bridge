#!/usr/bin/env bash
# Rebuild and replace viknow3-demo only (236 host network :5197).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TAG="${VIKNOW3_DEMO_TAG:-0.2.5-ops}"
IMAGE="viknow3-demo:${TAG}"

ENV_FILE="${VIKNOW3_RUNTIME_ENV:-/home/cursor-agent/viknow3/runtime.env}"
PG_ENV="${VIKNOW3_PG_ENV:-/home/cursor-agent/viknow3/pg-demo.env}"
OPS_ENV="${VIKNOW3_OPS_ENV:-/home/cursor-agent/.viknow3/ops.env}"
DATA_DIR="${VIKNOW3_HOST_DATA:-/home/cursor-agent/viknow3/data}"

cd "$REPO"
git submodule update --init external/HyperRAG

GITEA_KEY="${GITEA_SSH_KEY:-$HOME/.ssh/gitea_ljs_ed25519}"
SCHEDULER_DIR="$REPO/external/schedule-core"
if [[ ! -f "$SCHEDULER_DIR/pyproject.toml" ]]; then
  GIT_SSH_COMMAND="ssh -i ${GITEA_KEY} -o StrictHostKeyChecking=accept-new -p 2222" \
    git clone --depth 1 --branch v0.1.5 \
    ssh://git@git.qingxiang.tech:2222/castmeta-research/schedule-core.git \
    "$SCHEDULER_DIR"
fi
if [[ ! -f "$GITEA_KEY" ]]; then
  echo "Missing Gitea deploy key: $GITEA_KEY" >&2
  exit 1
fi

docker build \
  -f deploy/demo/Dockerfile \
  -t "$IMAGE" \
  "$REPO"

docker rm -f viknow3-demo 2>/dev/null || true
mkdir -p "$DATA_DIR"

docker run -d \
  --name viknow3-demo \
  --network host \
  --memory 4g \
  --cpus 2 \
  --env-file "$ENV_FILE" \
  --env-file "$PG_ENV" \
  --env-file "$OPS_ENV" \
  -e VIKNOW3_DATA_DIR=/data/viknow3 \
  -e VIKNOW3_ROOT=/opt/viknow3 \
  -v "${DATA_DIR}:/data/viknow3" \
  "$IMAGE"

echo "viknow3-demo -> http://127.0.0.1:5197 (image ${IMAGE})"
