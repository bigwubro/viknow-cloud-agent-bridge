#!/usr/bin/env bash
# Smoke: UI on PLAYGROUND_UI_BASE, ViKnow API on VIKNOW_API_BASE (scheme 2).
set -euo pipefail

UI_BASE="${PLAYGROUND_UI_BASE:-http://127.0.0.1:5198}"
API_BASE="${VIKNOW_API_BASE:-http://127.0.0.1:5218}"
APP_KEY="${VIKNOW3_APP_KEY:-dev-app-key}"
fail() { echo "FAIL: $*" >&2; exit 1; }

code() { curl -sf -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo "000"; }

for path in /playground /playground/workspace /playground/agents; do
  [[ "$(code "${UI_BASE}${path}")" == "200" ]] || fail "UI ${path} not 200"
done

cfg="$(curl -sf "${UI_BASE}/playground/assets/runtime-config.js")"
echo "$cfg" | grep -q VIKNOW_API_BASE || fail "runtime-config.js missing VIKNOW_API_BASE"

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

signed="$(curl -sf -X POST "${API_BASE}/api/v1/knowledge/libraries/${lib}/uploads" -H 'Content-Type: application/json' -H "X-App-Key: ${APP_KEY}" \
  -d '{"file_name":"note.txt","content_type":"text/plain"}')"
echo "$signed" | grep -q asset_path || fail "presign upload response"

echo "OK Playground UI ${UI_BASE} API ${API_BASE}"
