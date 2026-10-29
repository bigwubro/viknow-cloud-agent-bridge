#!/usr/bin/env bash
# Containerized ViKnow Platform + storage on 10.168.1.207 (Tailscale / LAN).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY207="$(cd "$(dirname "$0")" && pwd)"
DOCKER=("$DEPLOY207/docker.sh")
COMPOSE=("$DEPLOY207/docker.sh" compose)
HOST_ENV="${VIKNOW207_HOST_ENV:-${HOME}/viknow207.host.env}"

if [[ ! -f "$HOST_ENV" ]]; then
  echo "missing $HOST_ENV — copy deploy/207/host.env.example" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$HOST_ENV"
set +a

export VIKNOW_OPS_PG_CONTAINER="${VIKNOW_OPS_PG_CONTAINER:-viknow207-ops-postgres}"
export VIKNOW_PLATFORM_ROOT="${VIKNOW_PLATFORM_ROOT:-${HOME}/src/viknow-platform}"
export VIKNOW_STORAGE_ROOT="${VIKNOW_STORAGE_ROOT:-${HOME}/src/viknow-storage}"
export VIKNOW_DATA_DIR="${VIKNOW_DATA_DIR:-/data/viknow}"
export VIKNOW_PLATFORM_STATE="${VIKNOW_PLATFORM_STATE:-${HOME}/viknow-platform/state}"
export VIKNOW207_PLATFORM_ENV="${VIKNOW207_PLATFORM_ENV:-${HOME}/viknow207.platform.env}"
touch "$VIKNOW207_PLATFORM_ENV"
if [[ -n "${ACR_PASSWORD:-}" ]]; then
  bash "$DEPLOY207/acr-pull.sh"
else
  echo "skip ACR pull (set ACR_PASSWORD in host.env to pull)"
fi

echo "== Stop port conflicts =="
bash "$DEPLOY207/stop-port-conflicts.sh"

mkdir -p "${VIKNOW_DATA_DIR}/hyperrag-wd" "${VIKNOW_DATA_DIR}/scratch" \
  "${VIKNOW_DATA_DIR}/agent-workspaces" "${VIKNOW_PLATFORM_STATE}" \
  "${VIKNOW_STORAGE_MINIO_DATA:-${VIKNOW_DATA_DIR}/storage-minio}" \
  "${HOME}/viknow-platform" "${HOME}/.viknow-platform"
sudo mkdir -p "${VIKNOW_DATA_DIR}" 2>/dev/null || true
sudo chown -R "$(id -u):$(id -g)" "${VIKNOW_DATA_DIR}" "${VIKNOW_PLATFORM_STATE}" 2>/dev/null || true
chmod 777 "${VIKNOW_STORAGE_MINIO_DATA:-${VIKNOW_DATA_DIR}/storage-minio}" 2>/dev/null || true

echo "== Infra only (bootstrap Postgres) =="
"${COMPOSE[@]}" -f "$DEPLOY207/docker-compose.stack.yml" --env-file "$HOST_ENV" up -d postgres redis neo4j

echo "== Wait for Postgres =="
for _ in $(seq 1 60); do
  if "${DOCKER[@]}" exec "$VIKNOW_OPS_PG_CONTAINER" pg_isready -U viknow_ops -d viknow_ops >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
"${DOCKER[@]}" exec "$VIKNOW_OPS_PG_CONTAINER" pg_isready -U viknow_ops -d viknow_ops

VIKNOW_OPS_PG_CONTAINER="$VIKNOW_OPS_PG_CONTAINER" bash "$DEPLOY207/init-databases.sh"

echo "== Host pg-demo.env + ops.env =="
urlencode_pw() { python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"; }
EPW="$(urlencode_pw "$POSTGRES_PASSWORD")"
PG_DEMO="${HOME}/viknow-platform/pg-demo.env"
cat >"$PG_DEMO" <<EOF
VIKNOW_ACCESS_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_access
VIKNOW_AGENT_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_agent
VIKNOW_HYPERRAG_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_knowledge
EOF

OPS_ENV="${HOME}/.viknow-platform/ops.env"
cat >"$OPS_ENV" <<EOF
VIKNOW_OPS_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_ops
VIKNOW_OPS_SECRET=${VIKNOW_OPS_SECRET:?set VIKNOW_OPS_SECRET in host.env}
REDIS_PASSWORD=${REDIS_PASSWORD}
VIKNOW_REDIS_PORT=${VIKNOW_REDIS_PORT:-16479}
NEO4J_PASSWORD=${NEO4J_PASSWORD}
VIKNOW_NEO4J_PASSWORD=${NEO4J_PASSWORD}
EOF

echo "== Platform env files =="
mkdir -p "$ROOT/deploy/207/env"
if [[ "$DEPLOY207/env" -nt "$ROOT/deploy/207/env/api.env" ]] 2>/dev/null; then
  cp -f "$DEPLOY207/env/api.env" "$ROOT/deploy/207/env/api.env" 2>/dev/null || true
fi
if [[ ! -f "$ROOT/deploy/207/env/api.env" ]]; then
  cp -f "$DEPLOY207/env/api.env" "$ROOT/deploy/207/env/api.env"
fi
if [[ ! -f "$ROOT/deploy/207/env/ui.env" ]]; then
  cp -f "$DEPLOY207/env/ui.env" "$ROOT/deploy/207/env/ui.env"
fi
sed -i "s|/home/wuzihan|${HOME}|g" "$ROOT/deploy/207/env/api.env" 2>/dev/null || true

export VIKNOW236_HOST_ROOT="${HOME}"
export VIKNOW_DEPLOY_ENV="${VIKNOW_DEPLOY_ENV:-${HOME}/deploy.env}"
bash "$DEPLOY207/env/refresh-secrets-from-host.sh"

export VIKNOW_OPS_ENV="$OPS_ENV"
export VIKNOW_PG_ENV="$PG_DEMO"
export VIKNOW_REDIS_PORT="${VIKNOW_REDIS_PORT:-16479}"
REPAIR_PY="${VIKNOW_PYTHON:-python3}"
if [[ -x "${HOME}/viknow-platform/venv312/bin/python" ]]; then
  REPAIR_PY="${HOME}/viknow-platform/venv312/bin/python"
fi
if ! "$REPAIR_PY" -c "import psycopg" 2>/dev/null; then
  echo "Installing psycopg for ops repair (user pip)..."
  python3 -m pip install --user -q 'psycopg[binary]' cryptography
  REPAIR_PY=python3
fi

echo "== Bootstrap viknow_ops schema =="
PYTHONPATH="${ROOT}/src:${ROOT}" VIKNOW_OPS_DSN="$(grep ^VIKNOW_OPS_DSN= "$OPS_ENV" | cut -d= -f2-)" \
  "$REPAIR_PY" -c "from viknow_platform.ops.config.store import snapshot; snapshot(); print('ops schema OK')"

VIKNOW_PYTHON="$REPAIR_PY" VIKNOW_OPS_ENV="$OPS_ENV" VIKNOW_PG_ENV="$PG_DEMO" \
  VIKNOW_DEPLOY_ENV="${VIKNOW_DEPLOY_ENV:-${HOME}/deploy.env}" \
  bash "$ROOT/scripts/repair-ops-infra-secrets-from-host.sh"

# Container env: host network → 127.0.0.1 for infra + storage
{
  cat "$ROOT/deploy/207/env/api.env"
  cat "$ROOT/deploy/207/env/ui.env"
  grep -E '^VIKNOW_(ACCESS|AGENT|HYPERRAG|OPS)_DSN=' "$PG_DEMO" || true
  grep -E '^VIKNOW_OPS_DSN=' "$OPS_ENV" || true
  grep -E '^VIKNOW_OPS_SECRET=' "$OPS_ENV" || true
  grep -E '^VIKNOW_APP_KEY=|^VIKNOW_STORAGE_TOKEN=|^ACTIO_|^VIKNOW_PRIVATE_|^VIKNOW_EMBEDDING_' "$ROOT/deploy/207/env/secrets.env" 2>/dev/null || true
  echo "VIKNOW_DATA_DIR=${VIKNOW_DATA_DIR}"
  echo "VIKNOW_LIBRARIES_ROOT=${VIKNOW_DATA_DIR}/hyperrag-wd"
  echo "VIKNOW_SCRATCH_DIR=${VIKNOW_DATA_DIR}/scratch"
  echo "VIKNOW_AGENT_WORKSPACE_ROOT=${VIKNOW_DATA_DIR}/agent-workspaces"
  echo "VIKNOW_STATE_DIR=/state"
  echo "VIKNOW_VENDOR_FILES_ROOT=/state/vendor-files"
} | awk -F= '{order[++n]=$1; val[$1]=$0} END {for (i=1;i<=n;i++) if (val[order[i]] != "") print val[order[i]]}' >"$VIKNOW207_PLATFORM_ENV"

echo "== Storage + Platform containers (ACR pull only) =="
export MINIO_IMAGE="${MINIO_IMAGE:-registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-minio:latest}"
if [[ -z "${VIKNOW_PLATFORM_IMAGE:-}" || -z "${VIKNOW_STORAGE_IMAGE:-}" ]]; then
  echo "set VIKNOW_PLATFORM_IMAGE and VIKNOW_STORAGE_IMAGE in host.env (236: deploy/207/push-images-236.sh)" >&2
  exit 1
fi
"${COMPOSE[@]}" -f "$DEPLOY207/docker-compose.stack.yml" --env-file "$HOST_ENV" up -d --pull always minio storage platform

echo "== Smoke =="
for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:${VIKNOW_STORAGE_HTTP_PORT:-5219}/health" >/dev/null \
    && curl -sf -o /dev/null "http://127.0.0.1:5218/docs" \
    && curl -sf -o /dev/null "http://127.0.0.1:5198/playground"; then
    break
  fi
  sleep 3
done
curl -sf "http://127.0.0.1:${VIKNOW_STORAGE_HTTP_PORT:-5219}/health"
curl -sf -o /dev/null "http://127.0.0.1:5218/docs"
curl -sf -o /dev/null "http://127.0.0.1:5198/playground"
echo "207 container deploy OK — Playground http://${VIKNOW207_HOST_IP}:5198/playground"
