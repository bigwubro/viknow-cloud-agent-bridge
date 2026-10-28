#!/usr/bin/env bash
# Smoke acceptance: Playground UI (:5198) + ViKnow API (:5175) + viknow-storage.
set -euo pipefail

UI_BASE="${PLAYGROUND_BASE:-http://127.0.0.1:5198}"
API_BASE="${VIKNOW_API:-http://127.0.0.1:5175}"
STORAGE_URL="${VIKNOW_STORAGE_URL:-}"
STORAGE_TOKEN="${VIKNOW_STORAGE_TOKEN:-}"
APP_KEY="${VIKNOW3_APP_KEY:-dev-app-key}"

fail() { echo "FAIL: $*" >&2; exit 1; }

code() { curl -sf -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo "000"; }

for path in /playground /playground/workspace /playground/agents; do
  [[ "$(code "${UI_BASE}${path}")" == "200" ]] || fail "${path} not 200"
done

for path in /ui/playground/users /ui/playground/login /ui/playground/s3/presign-put; do
  c="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${UI_BASE}${path}" -H 'Content-Type: application/json' -d '{}')"
  [[ "$c" == "404" ]] || fail "legacy BFF ${path} expected 404 got ${c}"
done

[[ -n "$STORAGE_URL" && -n "$STORAGE_TOKEN" ]] || fail "set VIKNOW_STORAGE_URL and VIKNOW_STORAGE_TOKEN"

agents="$(curl -sf "${API_BASE}/api/v1/agent/agents" -H "X-App-Key: ${APP_KEY}")"
echo "$agents" | grep -q deepagents || fail "agent list missing deepagents"

user="$(curl -sf -X POST "${API_BASE}/api/v1/users" -H 'Content-Type: application/json' -d '{"display_name":"acceptance"}')"
uid="$(echo "$user" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf "${API_BASE}/api/v1/users/${uid}" >/dev/null || fail "GET user"

lib="pg-acc-$(date +%s)"
curl -sf -X POST "${STORAGE_URL%/}/libraries" \
  -H "Authorization: Bearer ${STORAGE_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"library_id\":\"${lib}\",\"title\":\"acceptance\"}" >/dev/null || fail "create library on storage"

curl -sf -X POST "${STORAGE_URL%/}/libraries/${lib}/assets" \
  -H "Authorization: Bearer ${STORAGE_TOKEN}" \
  -F 'file=@/etc/hosts;filename=note.txt' \
  -F 'asset_id=note-1' >/dev/null || fail "upload asset on storage"

curl -sf -X POST "${API_BASE}/api/v1/knowledge/index-jobs" \
  -H "X-App-Key: ${APP_KEY}" \
  -H 'Content-Type: application/json' \
  -d "{\"library_id\":\"${lib}\",\"asset_id\":\"note-1\",\"compile_mode\":\"fast_ingest\"}" >/dev/null || fail "submit index job"

echo "OK Playground acceptance UI=${UI_BASE} API=${API_BASE}"
