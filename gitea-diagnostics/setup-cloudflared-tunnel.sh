#!/usr/bin/env bash
# Expose 236 SSH via a public domain using Cloudflare Tunnel (cloudflared).
# Requires Gitea secret CLOUDFLARE_TUNNEL_TOKEN (from Zero Trust → Tunnels).
# Route in Cloudflare dashboard: e.g. ssh-236.qingxiang.tech -> tcp://localhost:22
set -euo pipefail

ACTION="${TUNNEL_ACTION:-install}"
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
UNIT_FILE="/etc/systemd/system/viknow-cloudflared.service"
BIN="/usr/local/bin/cloudflared"
CONFIG_DIR="/etc/viknow/cloudflared"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

install_cloudflared() {
  section "Install cloudflared"
  if [ -z "${TOKEN}" ]; then
    echo "CLOUDFLARE_TUNNEL_TOKEN secret is required" >&2
    exit 1
  fi
  mkdir -p "${CONFIG_DIR}"
  chmod 700 "${CONFIG_DIR}"
  if [ ! -x "${BIN}" ]; then
    arch="$(uname -m)"
    case "${arch}" in
      x86_64) cf_arch="amd64" ;;
      aarch64) cf_arch="arm64" ;;
      *) echo "unsupported arch ${arch}" >&2; exit 1 ;;
    esac
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}"
    curl -fsSL "${url}" -o "${BIN}"
    chmod 755 "${BIN}"
  fi
  "${BIN}" --version
  "${BIN}" service install "${TOKEN}"
  systemctl enable --now cloudflared
  sleep 2
  section "cloudflared status"
  systemctl --no-pager status cloudflared || true
  echo "Connect from Cloud Agent (after DNS route is configured in Cloudflare):"
  echo "  ssh cursor-agent@ssh-236.qingxiang.tech"
  echo "Use the hostname you configured in Cloudflare Zero Trust tunnel ingress."
}

show_status() {
  section "cloudflared status"
  systemctl --no-pager status cloudflared 2>/dev/null || echo "cloudflared service not installed"
  command -v cloudflared >/dev/null 2>&1 && cloudflared --version || true
}

stop_cloudflared() {
  section "Stop cloudflared"
  systemctl disable --now cloudflared 2>/dev/null || true
  echo "stopped"
}

require_root
case "${ACTION}" in
  install) install_cloudflared ;;
  status) show_status ;;
  stop) stop_cloudflared ;;
  *) echo "unknown TUNNEL_ACTION=${ACTION}" >&2; exit 1 ;;
esac
section "Done"
