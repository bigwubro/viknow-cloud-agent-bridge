#!/usr/bin/env bash
# Smoke: 207 storage grant URL → Actio fetch via 236 nginx proxy (run on 236 or agent with 236 SSH).
set -euo pipefail
LIB="${1:-207-e2e-10292328}"
ASSET="${2:-vid-3ca318e6}"
STORAGE_URL="${VIKNOW207_STORAGE_URL:-http://100.101.46.52:5219}"
TOKEN="${VIKNOW207_STORAGE_TOKEN:-${VIKNOW_STORAGE_TOKEN:-}}"
if [[ -z "$TOKEN" ]]; then
  echo "set VIKNOW207_STORAGE_TOKEN or VIKNOW_STORAGE_TOKEN" >&2
  exit 1
fi
issued="$(curl -sf -X POST -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  "${STORAGE_URL}/libraries/${LIB}/assets/${ASSET}/download-url" -d '{}')"
url="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["url"])' "$issued")"
echo "minted_host=$(python3 -c 'from urllib.parse import urlparse; import sys; print(urlparse(sys.argv[1]).netloc)' "$url")"
docker exec actio-server-8600 sh -c "cd /app && python3 - <<'PY'
import os, sys
sys.path.insert(0, '/app')
os.environ.setdefault('ACTIO_VIKNOW_GRANT_PROXY_BASE', 'http://172.21.0.1:5220')
from services.video_split.shared.media_input import rewrite_viknow_grant_url_for_actio, read_http_limited
raw = '''${url}'''
rew = rewrite_viknow_grant_url_for_actio(raw)
print('rewritten', rew)
data = read_http_limited(rew, max_bytes=50_000_000)
print('bytes', len(data))
PY"
echo OK
