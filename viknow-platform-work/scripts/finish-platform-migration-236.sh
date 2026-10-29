#!/usr/bin/env bash
# One-shot 236 host migration after viknow-platform rename. Run on 236 as cursor-agent.
set -euo pipefail

HOST_ROOT="${HOME}"
SRC_OLD="${VIKNOW236_SRC_OLD:-$HOST_ROOT/src/viknow3}"
SRC_NEW="${VIKNOW236_SRC:-$HOST_ROOT/src/viknow-platform}"
DATA_OLD="${VIKNOW236_DATA_OLD:-$HOST_ROOT/viknow3}"
DATA_NEW="${VIKNOW236_DATA:-$HOST_ROOT/viknow-platform}"
GITEA_REPO="${VIKNOW_GITEA_REPO:-ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow-platform.git}"

echo "== Symlinks (legacy paths -> viknow-platform) =="
if [[ -d "$SRC_OLD" && ! -e "$SRC_NEW" ]]; then
  ln -sfn "$SRC_OLD" "$SRC_NEW"
elif [[ -d "$SRC_NEW" && ! -e "$SRC_OLD" ]]; then
  ln -sfn "$SRC_NEW" "$SRC_OLD"
fi
if [[ -d "$DATA_OLD" && ! -e "$DATA_NEW" ]]; then
  ln -sfn "$DATA_OLD" "$DATA_NEW"
elif [[ -d "$DATA_NEW" && ! -e "$DATA_OLD" ]]; then
  ln -sfn "$DATA_NEW" "$DATA_OLD"
fi

rewrite_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  sed -i \
    -e 's/^VIKNOW3_APP_KEY=/VIKNOW_APP_KEY=/' \
    -e 's/^VIKNOW3_DATA_DIR=/VIKNOW_DATA_DIR=/' \
    -e 's/^VIKNOW3_LIBRARIES_ROOT=/VIKNOW_LIBRARIES_ROOT=/' \
    -e 's/^VIKNOW3_ROOT=/VIKNOW_PLATFORM_ROOT=/' \
    -e 's/^VIKNOW3_ENV_FILE=/VIKNOW_ENV_FILE=/' \
    -e 's/^VIKNOW3_PG_ENV=/VIKNOW_PG_ENV=/' \
    -e 's/^VIKNOW3_OPS_ENV=/VIKNOW_OPS_ENV=/' \
    -e 's/^VIKNOW3_CHAT_/VIKNOW_CHAT_/' \
    -e 's/^VIKNOW3_EMBED_/VIKNOW_EMBED_/' \
    "$f"
  echo "rewrote keys in $f"
}

echo "== Rewrite legacy VIKNOW3_* in host env files =="
for f in \
  "$DATA_NEW/pg-demo.env" "$DATA_OLD/pg-demo.env" \
  "$DATA_NEW/demo.env" "$DATA_OLD/demo.env" \
  "$HOST_ROOT/.viknow-platform/ops.env" "$HOST_ROOT/.viknow3/ops.env"; do
  rewrite_env_file "$f"
done

REPO="$SRC_NEW"
[[ -d "$REPO" ]] || REPO="$SRC_OLD"
[[ -d "$REPO" ]] || { echo "missing repo under $SRC_NEW" >&2; exit 1; }

cd "$REPO"
git fetch origin 2>/dev/null || true
current_url="$(git remote get-url origin 2>/dev/null || true)"
if [[ "$current_url" != "$GITEA_REPO" ]]; then
  git remote set-url origin "$GITEA_REPO" || echo "warn: could not set origin to $GITEA_REPO"
fi

echo "== Refresh deploy/236/env/secrets.env =="
bash deploy/236/env/refresh-secrets-from-host.sh
rewrite_env_file deploy/236/env/secrets.env

echo "== Repair viknow_ops infra secrets (Fernet) from host env =="
bash scripts/repair-ops-infra-secrets-from-host.sh
rm -f "${DATA_NEW}/state/hyperrag/hyperrag.yml" "${DATA_OLD}/state/hyperrag/hyperrag.yml" 2>/dev/null || true

echo "== Restart Playground (host processes) =="
docker rm -f viknow3-playground viknow-platform-playground 2>/dev/null || true
ln -sfn "${DATA_OLD}/venv312" "${DATA_NEW}/venv312" 2>/dev/null || ln -sfn "${HOST_ROOT}/viknow3/venv312" "${DATA_NEW}/venv312" 2>/dev/null || true
export VIKNOW_PYTHON="${VIKNOW_PYTHON:-${DATA_OLD}/venv312/bin/python}"
bash scripts/deploy-playground-ui.sh restart

echo "== Smoke =="
ready=0
for _ in $(seq 1 90); do
  if curl -sf -o /dev/null "http://127.0.0.1:5198/playground" \
     && curl -sf -o /dev/null "http://127.0.0.1:5218/docs"; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  echo "Playground smoke failed; tail logs:" >&2
  tail -30 /tmp/viknow-api.log >&2 || true
  tail -15 /tmp/viknow-ui.log >&2 || true
  exit 1
fi
