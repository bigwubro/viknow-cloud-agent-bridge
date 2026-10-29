#!/usr/bin/env bash
# Package deploy/207 + viknow-storage for 10.168.1.207 (run on 236 when 207 is on LAN).
set -euo pipefail
STAGE="${HOME}/staging/viknow207-deploy"
mkdir -p "$STAGE"
PLATFORM="${VIKNOW_PLATFORM_SRC:-$HOME/src/viknow3}"
STORAGE="${VIKNOW_STORAGE_SRC:-$HOME/src/viknow-storage}"
tar czf "$STAGE/viknow207-deploy.tgz" \
  -C "$PLATFORM" deploy/207 scripts/deploy-playground-ui.sh scripts/repair-ops-infra-secrets-from-host.sh \
  -C "$STORAGE" . 2>/dev/null || {
  echo "fallback: pack from bridge paths if synced" >&2
  exit 1
}
echo "Wrote $STAGE/viknow207-deploy.tgz"
echo "On 207: tar xzf viknow207-deploy.tgz -C ~/src/viknow-platform && ..."
