#!/usr/bin/env bash
# Deploy Playground container on 236 (host network). Called from Gitea Actions or manually.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

IMAGE="${1:-}"
if [[ -z "$IMAGE" ]]; then
  echo "usage: $0 <docker-image-ref>" >&2
  exit 2
fi

CONTAINER="${VIKNOW_PLAYGROUND_CONTAINER:-viknow-platform-playground}"
API_ENV="$ROOT/deploy/236/env/api.env"
UI_ENV="$ROOT/deploy/236/env/ui.env"
SECRETS_ENV="$ROOT/deploy/236/env/secrets.env"

for f in "$API_ENV" "$UI_ENV" "$SECRETS_ENV"; do
  [[ -f "$f" ]] || { echo "missing $f (see deploy/236/env/ENV.md)" >&2; exit 1; }
done

echo "Stopping host Playground processes (if any)..."
if [[ -x "$ROOT/scripts/deploy-playground-ui.sh" ]]; then
  bash "$ROOT/scripts/deploy-playground-ui.sh" stop || true
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
fi

echo "Starting $CONTAINER from $IMAGE (env: api.env + secrets.env + ui.env)"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  --group-add 999 \
  --security-opt seccomp=unconfined \
  --privileged \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /data/viknow:/data/viknow \
  -v /home/cursor-agent/viknow-platform/state:/state \
  --env-file "$API_ENV" \
  --env-file "$SECRETS_ENV" \
  --env-file "$UI_ENV" \
  "$IMAGE"

ready=0
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:5198/playground" \
     && curl -sf -o /dev/null "http://127.0.0.1:5218/docs"; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  echo "Playground not ready; docker logs $CONTAINER:" >&2
  docker logs --tail 80 "$CONTAINER" >&2 || true
  exit 1
fi
echo "Playground OK: http://36.103.198.236:5198/playground API :5218"
