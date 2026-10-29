#!/usr/bin/env bash
# Join Tailscale tailnet from Cloud Agent VM (no systemd) and verify path to 207 / qx-ai-07.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUTH_MD="$ROOT/docs/207-tailscale-auth.md"
if [[ -z "${VIKNOW_TAILSCALE_AUTH_KEY:-}" && -f "$AUTH_MD" ]]; then
  VIKNOW_TAILSCALE_AUTH_KEY="$(grep -oE 'tskey-auth-[A-Za-z0-9-]+' "$AUTH_MD" | head -1)"
fi
AUTH_KEY="${VIKNOW_TAILSCALE_AUTH_KEY:?set VIKNOW_TAILSCALE_AUTH_KEY or docs/207-tailscale-auth.md}"
HOSTNAME="${VIKNOW_TAILSCALE_HOSTNAME:-cursor-cloud-viknow-agent}"
SOCK="/var/run/tailscale/tailscaled.sock"
STATE="/var/lib/tailscale/tailscaled.state"
LOG="/tmp/tailscaled.log"

sudo mkdir -p /var/run/tailscale /var/lib/tailscale

if ! sudo test -S "$SOCK" 2>/dev/null; then
  echo "Starting tailscaled (background)..."
  sudo nohup tailscaled --state="$STATE" --socket="$SOCK" >>"$LOG" 2>&1 &
  for _ in $(seq 1 30); do
    sudo test -S "$SOCK" && break
    sleep 0.5
  done
fi

sudo tailscale up --reset --accept-routes --hostname="$HOSTNAME" --auth-key="$AUTH_KEY"

echo "=== tailscale status ==="
sudo tailscale status || true
echo "=== ping qx-ai-07 (Tailscale IP) ==="
ping -c 2 -W 4 100.101.46.52 || true
echo "=== ping 10.168.1.207 (needs subnet route approved on qx-ai-07) ==="
ping -c 2 -W 4 10.168.1.207 || true
echo "OK: prefer SSH target wuzihan@100.101.46.52 until 10.168.1.0/24 route is approved in admin console"
