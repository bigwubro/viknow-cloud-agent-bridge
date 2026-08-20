#!/usr/bin/env bash
# Expose 236 SSH via outbound reverse tunnel (no inbound / no ngrok account required).
#
# SAFETY: This script does NOT modify sshd, iptables, or the :22 listener.
# It only starts an outbound client that forwards tunnel traffic to 127.0.0.1:22.
# Existing users connecting to 36.103.198.236:22 are unaffected.
#   serveo - default, uses system ssh -R (no account, no binary download)
#   bore   - bore.pub (needs github release download)
#   localhost - ssh -R via localhost.run (no account)
set -euo pipefail

ACTION="${TUNNEL_ACTION:-install}"
BACKEND="${TUNNEL_BACKEND:-serveo}"
LOCAL_PORT="${TUNNEL_LOCAL_PORT:-22}"
TUNNEL_NAME="${TUNNEL_NAME:-viknow236}"
BIN_DIR="/usr/local/bin"
UNIT_FILE="/etc/systemd/system/viknow-reverse-tunnel.service"
STATE_DIR="/etc/viknow/reverse-tunnel"
LOG_FILE="${STATE_DIR}/tunnel.log"

section() { printf '\n========== %s ==========\n' "$*"; }

verify_port22_safe() {
  section "Safety check: :22 must remain sshd-owned"
  if ! ss -tlnp | grep -E ':22\b' | grep -q sshd; then
    echo "ERROR: sshd is not listening on :22; aborting to protect existing access" >&2
    exit 1
  fi
  echo "OK: sshd still listens on :22 (tunnel will only connect to 127.0.0.1:${LOCAL_PORT})"
  echo "NOTE: this script does not change sshd_config, iptables, or port 22 binding"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

install_bore() {
  local bin="${BIN_DIR}/bore"
  if [ ! -x "${bin}" ]; then
    arch="$(uname -m)"
    case "${arch}" in
      x86_64) asset="bore-linux-amd64" ;;
      aarch64) asset="bore-linux-arm64" ;;
      *) echo "unsupported arch ${arch}" >&2; exit 1 ;;
    esac
    urls=(
      "http://101.71.223.113:3000/castmeta-research/viknow2/raw/branch/agent/cloud-agent-tunnel/gitea-diagnostics/bin/${asset}"
      "http://git.qingxiang.tech:3000/castmeta-research/viknow2/raw/branch/agent/cloud-agent-tunnel/gitea-diagnostics/bin/${asset}"
      "https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz"
    )
    for url in "${urls[@]}"; do
      echo "try download ${url}"
      if [[ "${url}" == *.tar.gz ]]; then
        tmp="$(mktemp)"
        if curl -fsSL --connect-timeout 30 "${url}" -o "${tmp}.tgz"; then
          tar xzf "${tmp}.tgz" -C /tmp bore 2>/dev/null && mv /tmp/bore "${bin}" && chmod 755 "${bin}" && rm -f "${tmp}.tgz" && break
        fi
      elif curl -fsSL --connect-timeout 30 "${url}" -o "${bin}"; then
        chmod 755 "${bin}"
        break
      fi
    done
    [ -x "${bin}" ] || { echo "failed to download bore" >&2; exit 1; }
  fi
  "${bin}" --version 2>/dev/null || true
}

install_ngrok() {
  local bin="${BIN_DIR}/ngrok"
  if [ ! -x "${bin}" ]; then
    arch="$(uname -m)"
    case "${arch}" in
      x86_64) cf_arch="amd64" ;;
      aarch64) cf_arch="arm64" ;;
      *) echo "unsupported arch ${arch}" >&2; exit 1 ;;
    esac
    curl -fsSL "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-${cf_arch}.tgz" | tar xz -C /tmp ngrok
    mv /tmp/ngrok "${bin}"
    chmod 755 "${bin}"
  fi
  "${bin}" version
}

write_localhost_unit() {
  cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=ViKnow reverse SSH tunnel via localhost.run
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -R 22222:127.0.0.1:${LOCAL_PORT} ssh.localhost.run
Restart=always
RestartSec=15
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF
}

write_serveo_unit() {
  cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=ViKnow reverse SSH tunnel via serveo.net
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -R ${TUNNEL_NAME}:22:127.0.0.1:${LOCAL_PORT} serveo.net
Restart=always
RestartSec=10
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF
}

write_bore_unit() {
  cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=ViKnow reverse SSH tunnel via bore.pub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${BIN_DIR}/bore local ${LOCAL_PORT} --to bore.pub
Restart=always
RestartSec=5
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF
}

write_ngrok_unit() {
  if [ -z "${NGROK_AUTHTOKEN:-}" ]; then
    echo "NGROK_AUTHTOKEN required for ngrok backend" >&2
    exit 1
  fi
  mkdir -p "${STATE_DIR}"
  chmod 700 "${STATE_DIR}"
  printf '%s\n' "${NGROK_AUTHTOKEN}" > "${STATE_DIR}/ngrok.token"
  chmod 600 "${STATE_DIR}/ngrok.token"
  cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=ViKnow reverse SSH tunnel via ngrok
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NGROK_AUTHTOKEN=${NGROK_AUTHTOKEN}
ExecStart=${BIN_DIR}/ngrok tcp ${LOCAL_PORT} --log stdout
Restart=always
RestartSec=5
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF
}

install_tunnel() {
  section "Install reverse tunnel (${BACKEND})"
  verify_port22_safe
  mkdir -p "${STATE_DIR}"
  chmod 700 "${STATE_DIR}"
  touch "${LOG_FILE}"
  chmod 600 "${LOG_FILE}"
  systemctl disable --now viknow-reverse-tunnel.service 2>/dev/null || true

  case "${BACKEND}" in
    localhost)
      write_localhost_unit
      ;;
    serveo)
      write_serveo_unit
      ;;
    bore)
      if [ -x "${BIN_DIR}/bore" ]; then
        echo "bore already installed at ${BIN_DIR}/bore"
      else
        install_bore
      fi
      write_bore_unit
      ;;
    ngrok)
      install_ngrok
      write_ngrok_unit
      ;;
    *)
      echo "unknown TUNNEL_BACKEND=${BACKEND}" >&2
      exit 1
      ;;
  esac

  systemctl daemon-reload
  systemctl enable --now viknow-reverse-tunnel.service
  sleep 6
  verify_port22_safe
  show_status
}

show_status() {
  section "Service status"
  systemctl --no-pager status viknow-reverse-tunnel.service || true
  section "Tunnel log (last 30 lines)"
  tail -n 30 "${LOG_FILE}" 2>/dev/null || echo "(no log yet)"
  section "Connection hint"
  if grep -qi localhost.run "${LOG_FILE}" 2>/dev/null; then
    host="$(grep -Eo '[a-z0-9.-]+\.localhost\.run' "${LOG_FILE}" 2>/dev/null | tail -1 || true)"
    if [ -n "${host}" ]; then
      echo "Cloud Agent: ssh -p 22222 cursor-agent@${host}"
      echo "REVERSE_TUNNEL_HOST=${host}"
      echo "REVERSE_TUNNEL_PORT=22222"
    fi
  fi
  if [ "${BACKEND}" = "serveo" ] || grep -qi serveo "${LOG_FILE}" 2>/dev/null; then
    echo "Cloud Agent: ssh cursor-agent@${TUNNEL_NAME}.serveo.net"
    echo "REVERSE_TUNNEL_HOST=${TUNNEL_NAME}.serveo.net"
    echo "REVERSE_TUNNEL_PORT=22"
  fi
  if [ "${BACKEND}" = "bore" ] || grep -q bore.pub "${LOG_FILE}" 2>/dev/null; then
    port="$(grep -Eo 'bore\.pub:[0-9]+' "${LOG_FILE}" 2>/dev/null | tail -1 | cut -d: -f2 || true)"
    if [ -n "${port}" ]; then
      echo "Cloud Agent: ssh -p ${port} cursor-agent@bore.pub"
      echo "REVERSE_TUNNEL_HOST=bore.pub"
      echo "REVERSE_TUNNEL_PORT=${port}"
    else
      echo "Tunnel starting; re-run action=status in ~10s"
    fi
  fi
  if grep -q 'tcp://' "${LOG_FILE}" 2>/dev/null; then
    grep -Eo 'tcp://[0-9a-zA-Z.:.-]+' "${LOG_FILE}" | tail -1 || true
  fi
  section "cursor-agent local test"
  key="/etc/viknow/cloud-agent-cursor/id_ed25519"
  if [ -f "${key}" ]; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p "${LOCAL_PORT}" -i "${key}" cursor-agent@127.0.0.1 'echo local_ok' 2>&1 || true
  fi
}

stop_tunnel() {
  section "Stop reverse tunnel"
  systemctl disable --now viknow-reverse-tunnel.service 2>/dev/null || true
  echo "stopped"
}

require_root
case "${ACTION}" in
  install) install_tunnel ;;
  status) show_status ;;
  stop) stop_tunnel ;;
  *)
    echo "unknown TUNNEL_ACTION=${ACTION}" >&2
    exit 1
    ;;
esac
section "Done"
