#!/usr/bin/env bash
# Full stack on 10.168.1.207: infra + viknow-storage + Playground (host processes).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY207="$(cd "$(dirname "$0")" && pwd)"
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
export VIKNOW_STORAGE_ROOT="${VIKNOW_STORAGE_ROOT:-${HOME}/src/viknow-storage}"
export VIKNOW_DATA_DIR="${VIKNOW_DATA_DIR:-/data/viknow}"
export VIKNOW_PLATFORM_STATE="${VIKNOW_PLATFORM_STATE:-${HOME}/viknow-platform/state}"

echo "== ACR pull =="
if [[ -n "${ACR_PASSWORD:-}" ]]; then
  bash "$DEPLOY207/acr-pull.sh"
else
  echo "skip ACR pull (set ACR_PASSWORD in host.env to pull)"
fi

echo "== Stop port conflicts =="
bash "$DEPLOY207/stop-port-conflicts.sh"

echo "== Infra (PG / Redis / Neo4j) =="
docker compose -f "$DEPLOY207/docker-compose.infra.yml" --env-file "$HOST_ENV" up -d

echo "== Wait for Postgres =="
for _ in $(seq 1 60); do
  if docker exec "$VIKNOW_OPS_PG_CONTAINER" pg_isready -U viknow_ops -d viknow_ops >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker exec "$VIKNOW_OPS_PG_CONTAINER" pg_isready -U viknow_ops -d viknow_ops

bash "$DEPLOY207/init-databases.sh"

echo "== Host pg-demo.env + ops.env =="
mkdir -p "${HOME}/viknow-platform" "${VIKNOW_DATA_DIR}/hyperrag-wd" "${VIKNOW_DATA_DIR}/scratch" \
  "${VIKNOW_DATA_DIR}/agent-workspaces" "${VIKNOW_PLATFORM_STATE}" "${VIKNOW_STORAGE_MINIO_DATA:-${VIKNOW_DATA_DIR}/storage-minio}"

PG_DEMO="${HOME}/viknow-platform/pg-demo.env"
urlencode_pw() { python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"; }
EPW="$(urlencode_pw "$POSTGRES_PASSWORD")"
cat >"$PG_DEMO" <<EOF
VIKNOW_ACCESS_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_access
VIKNOW_AGENT_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_agent
VIKNOW_HYPERRAG_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_knowledge
EOF

OPS_ENV="${HOME}/.viknow-platform/ops.env"
mkdir -p "${HOME}/.viknow-platform"
cat >"$OPS_ENV" <<EOF
VIKNOW_OPS_DSN=postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_ops
VIKNOW_OPS_SECRET=${VIKNOW_OPS_SECRET:?set VIKNOW_OPS_SECRET in host.env}
REDIS_PASSWORD=${REDIS_PASSWORD}
VIKNOW_REDIS_PORT=${VIKNOW_REDIS_PORT:-16479}
NEO4J_PASSWORD=${NEO4J_PASSWORD}
VIKNOW_NEO4J_PASSWORD=${NEO4J_PASSWORD}
EOF

echo "== viknow-storage =="
if [[ ! -d "$VIKNOW_STORAGE_ROOT" ]]; then
  echo "missing $VIKNOW_STORAGE_ROOT — clone viknow-storage repo first" >&2
  exit 1
fi
STORAGE_ENV="$VIKNOW_STORAGE_ROOT/deploy/storage.env"
STORAGE_DSN="postgresql://viknow_ops:${EPW}@127.0.0.1:${VIKNOW_PG_PORT:-16432}/viknow_storage"
cat >"$STORAGE_ENV" <<EOF
STORAGE_TOKEN=${STORAGE_TOKEN:?set STORAGE_TOKEN}
STORAGE_DATABASE_URL=${STORAGE_DSN}
VIKNOW_STORAGE_MINIO_DATA=${VIKNOW_STORAGE_MINIO_DATA:-${VIKNOW_DATA_DIR}/storage-minio}
VIKNOW_STORAGE_HTTP_PORT=${VIKNOW_STORAGE_HTTP_PORT:-5219}
MINIO_IMAGE=registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-minio:latest
STORAGE_CORS_ORIGINS=http://${VIKNOW207_HOST_IP:?set VIKNOW207_HOST_IP}:5198
VIKNOW_OPS_PG_CONTAINER=${VIKNOW_OPS_PG_CONTAINER}
EOF
chmod 777 "${VIKNOW_STORAGE_MINIO_DATA:-${VIKNOW_DATA_DIR}/storage-minio}" 2>/dev/null || true
bash "$VIKNOW_STORAGE_ROOT/deploy/init-storage-database.sh"
(
  cd "$VIKNOW_STORAGE_ROOT"
  docker compose --env-file "$STORAGE_ENV" up -d --build --pull never
)
curl -sf "http://127.0.0.1:${VIKNOW_STORAGE_HTTP_PORT:-5219}/health"

echo "== Platform env (207 deploy files + secrets refresh) =="
mkdir -p "$ROOT/deploy/207/env"
cp -f "$DEPLOY207/env/api.env" "$ROOT/deploy/207/env/api.env"
cp -f "$DEPLOY207/env/ui.env" "$ROOT/deploy/207/env/ui.env"
sed -i "s|/home/wuzihan|${HOME}|g" "$ROOT/deploy/207/env/api.env" 2>/dev/null || \
  sed -i '' "s|/home/wuzihan|${HOME}|g" "$ROOT/deploy/207/env/api.env" 2>/dev/null || true

export VIKNOW236_HOST_ROOT="${HOME}"
export VIKNOW_DEPLOY_ENV="${VIKNOW_DEPLOY_ENV:-${HOME}/deploy.env}"
bash "$DEPLOY207/env/refresh-secrets-from-host.sh"

export VIKNOW_OPS_ENV="$OPS_ENV"
export VIKNOW_PG_ENV="$PG_DEMO"
export VIKNOW_DEPLOY_ENV="${VIKNOW_DEPLOY_ENV:-${HOME}/deploy.env}"
export VIKNOW_REDIS_PORT="${VIKNOW_REDIS_PORT:-16479}"
bash "$ROOT/scripts/repair-ops-infra-secrets-from-host.sh"

echo "== Playground (host processes) =="
export VIKNOW_API_ENV_FILE="$ROOT/deploy/207/env/api.env"
export VIKNOW_UI_ENV_FILE="$ROOT/deploy/207/env/ui.env"
export VIKNOW_SECRETS_ENV_FILE="$ROOT/deploy/207/env/secrets.env"
export VIKNOW_ENV_FILE="${HOME}/viknow-platform/pg-demo.env:${HOME}/deploy.env:${HOME}/.viknow-platform/ops.env"
export VIKNOW_PYTHON="${VIKNOW_PYTHON:-${HOME}/viknow-platform/venv312/bin/python}"
if [[ ! -x "$VIKNOW_PYTHON" ]]; then
  echo "missing venv at $VIKNOW_PYTHON — create venv312 and pip install -e . on 207" >&2
  exit 1
fi
bash "$ROOT/scripts/deploy-playground-ui.sh" restart

echo "== Smoke =="
curl -sf "http://127.0.0.1:${VIKNOW_STORAGE_HTTP_PORT:-5219}/health"
curl -sf -o /dev/null "http://127.0.0.1:5218/docs"
curl -sf -o /dev/null "http://127.0.0.1:5198/playground"
echo "207 deploy OK — Playground http://${VIKNOW207_HOST_IP}:5198/playground"
