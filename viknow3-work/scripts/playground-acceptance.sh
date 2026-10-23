#!/usr/bin/env bash
# Smoke acceptance: Playground on 236 uses /api/v1/* only (agent chat mock in browser).
set -euo pipefail

BASE="${PLAYGROUND_BASE:-http://127.0.0.1:5198}"
fail() { echo "FAIL: $*" >&2; exit 1; }

code() { curl -sf -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo "000"; }

for path in /playground /playground/workspace /playground/agents; do
  [[ "$(code "${BASE}${path}")" == "200" ]] || fail "${path} not 200"
done

for path in /ui/playground/users /ui/playground/login /ui/playground/s3/presign-put; do
  c="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}${path}" -H 'Content-Type: application/json' -d '{}')"
  [[ "$c" == "404" ]] || fail "legacy BFF ${path} expected 404 got ${c}"
done

agents="$(curl -sf "${BASE}/api/v1/agent/agents")"
echo "$agents" | grep -q deepagents || fail "agent list missing deepagents"
echo "$agents" | grep -q '"deepagents"' || fail "agent list missing deepagents"

user="$(curl -sf -X POST "${BASE}/api/v1/users" -H 'Content-Type: application/json' -d '{"display_name":"acceptance"}')"
uid="$(echo "$user" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf "${BASE}/api/v1/users/${uid}" >/dev/null || fail "GET user"

lib="pg-acc-$(date +%s)"
curl -sf -X POST "${BASE}/api/v1/knowledge/libraries" -H 'Content-Type: application/json' \
  -d "{\"library_id\":\"${lib}\",\"title\":\"acceptance\"}" >/dev/null || fail "create library"

signed="$(curl -sf -X POST "${BASE}/api/v1/knowledge/libraries/${lib}/uploads" -H 'Content-Type: application/json' \
  -d '{"file_name":"note.txt","content_type":"text/plain"}')"
echo "$signed" | grep -q asset_path || fail "presign upload response"

echo "OK Playground API acceptance at ${BASE}"
