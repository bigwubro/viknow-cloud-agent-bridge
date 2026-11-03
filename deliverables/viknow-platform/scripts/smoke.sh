#!/usr/bin/env bash
set -euo pipefail
PORT="${VIKNOW_HOST_PORT:-8000}"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/docs" || true)"
echo "GET /docs -> HTTP $code"
if [[ "$code" != "200" ]]; then
  exit 1
fi
