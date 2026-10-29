#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/website"
DEPLOY_ROOT="${VIKNOW_DOCS_DEPLOY_ROOT:-/data/wuzihan/docusaurus}"
BUILD_SRC="$WEB/build"
BUILD_DST="$DEPLOY_ROOT/site/build"

default_npm_cache="/cache/npm"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$default_npm_cache}"
if ! mkdir -p "$NPM_CONFIG_CACHE" 2>/dev/null; then
  NPM_CONFIG_CACHE="${HOME}/.cache/viknow-platform-npm"
  mkdir -p "$NPM_CONFIG_CACHE"
  export NPM_CONFIG_CACHE
fi

cd "$ROOT"
python3 scripts/export_openapi.py

cd "$WEB"
npm ci
npm run build

mkdir -p "$BUILD_DST"
rsync -a --delete "$BUILD_SRC/" "$BUILD_DST/"

if [[ -f "$DEPLOY_ROOT/docker-compose.yml" ]]; then
  (cd "$DEPLOY_ROOT" && docker compose up -d)
fi

curl -fsS -o /dev/null "http://127.0.0.1:3022/"
echo "OK: http://36.103.198.236:3022/"
