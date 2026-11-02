#!/usr/bin/env bash
# Smoke: UI on PLAYGROUND_UI_BASE, ViKnow API on VIKNOW_API_BASE (scheme 2).
set -euo pipefail

UI_BASE="${PLAYGROUND_UI_BASE:-http://127.0.0.1:5198}"
API_BASE="${VIKNOW_API_BASE:-http://127.0.0.1:5218}"
STORAGE_BASE="${VIKNOW_STORAGE_URL:-http://127.0.0.1:5219}"
SECRETS_FILE="${VIKNOW_SECRETS_ENV_FILE:-deploy/236/env/secrets.env}"
APP_KEY="${VIKNOW_APP_KEY:-dev-app-key}"
STORAGE_TOKEN="${VIKNOW_STORAGE_TOKEN:-}"
if [[ -z "$STORAGE_TOKEN" && -f "$SECRETS_FILE" ]]; then
  STORAGE_TOKEN="$(grep -m1 '^VIKNOW_STORAGE_TOKEN=' "$SECRETS_FILE" | cut -d= -f2- || true)"
fi
fail() { echo "FAIL: $*" >&2; exit 1; }

code() { curl -sf -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo "000"; }

for path in /playground /playground/workspace /playground/agents; do
  [[ "$(code "${UI_BASE}${path}")" == "200" ]] || fail "UI ${path} not 200"
done

cfg="$(curl -sf "${UI_BASE}/playground/assets/runtime-config.js")"
echo "$cfg" | grep -q VIKNOW_API_BASE || fail "runtime-config.js missing VIKNOW_API_BASE"
echo "$cfg" | grep -q VIKNOW_STORAGE_URL || fail "runtime-config.js missing VIKNOW_STORAGE_URL"
echo "$cfg" | grep -q VIKNOW_STORAGE_TOKEN || fail "runtime-config.js missing VIKNOW_STORAGE_TOKEN"

for path in /ui/playground/users /ui/playground/login /ui/playground/s3/presign-put; do
  c="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${UI_BASE}${path}" -H 'Content-Type: application/json' -d '{}')"
  [[ "$c" == "404" ]] || fail "removed route ${path} expected 404 got ${c}"
done

agents="$(curl -sf -H "X-App-Key: ${APP_KEY}" "${API_BASE}/api/v1/agent/agents")"
echo "$agents" | grep -q deepagents || fail "agent list missing deepagents"

user="$(curl -sf -X POST "${API_BASE}/api/v1/users" -H "Content-Type: application/json" -H "X-App-Key: ${APP_KEY}" -d '{"display_name":"acceptance"}')"
uid="$(echo "$user" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -H "X-App-Key: ${APP_KEY}" "${API_BASE}/api/v1/users/${uid}" >/dev/null || fail "GET user"

lib="pg-acc-$(date +%s)"
curl -sf -X POST "${API_BASE}/api/v1/knowledge/libraries" -H 'Content-Type: application/json' -H "X-App-Key: ${APP_KEY}" \
  -d "{\"library_id\":\"${lib}\",\"title\":\"acceptance\"}" >/dev/null || fail "create library"

aid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -sf -X POST "${STORAGE_BASE}/libraries/${lib}/assets" \
  -H "Authorization: Bearer ${STORAGE_TOKEN}" \
  -F "asset_id=${aid}" \
  -F "file=@/etc/hosts;filename=note.txt;type=text/plain" >/dev/null || fail "storage upload"

asset_path="knowledge/assets/${aid}/upload/${aid}.txt"
curl -sf -X POST "${API_BASE}/api/v1/knowledge/libraries/${lib}/assets" \
  -H 'Content-Type: application/json' -H "X-App-Key: ${APP_KEY}" \
  -d "{\"asset_id\":\"${aid}\",\"asset_path\":\"${asset_path}\",\"title\":\"note.txt\"}" >/dev/null || fail "create asset"

curl -sf -H "Authorization: Bearer ${STORAGE_TOKEN}" \
  "${STORAGE_BASE}/libraries/${lib}/assets/${aid}/preview" >/dev/null || fail "storage preview"

echo "OK Playground UI ${UI_BASE} API ${API_BASE} storage ${STORAGE_BASE}"
