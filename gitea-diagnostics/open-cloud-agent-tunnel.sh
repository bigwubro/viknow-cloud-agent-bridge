#!/usr/bin/env bash
# Configure a Cloud Agent entry path onto this host (236).
# Run on the 236 rootfs (via CI chroot). Two modes:
#   direct - open a dedicated sshd port + iptables allowlist for Cloud Agent IP
#   relay  - persistent reverse SSH tunnel to Gitea relay host (101.71.223.113)
set -euo pipefail

ACTION="${TUNNEL_ACTION:-install}"
MODE="${TUNNEL_MODE:-relay}"
TUNNEL_PORT="${CLOUD_AGENT_TUNNEL_PORT:-42236}"
RELAY_HOST="${CLOUD_AGENT_TUNNEL_HOST:-git.qingxiang.tech}"
RELAY_USER="${CLOUD_AGENT_TUNNEL_USER:-tunnel}"
CLOUD_AGENT_CIDR="${CLOUD_AGENT_CIDR:-}"
SSHD_DROPIN="/etc/ssh/sshd_config.d/99-cloud-agent-port.conf"
UNIT_FILE="/etc/systemd/system/viknow-cloud-agent-tunnel.service"
KEY_DIR="/etc/viknow/cloud-agent-tunnel"
KEY_FILE="${KEY_DIR}/id_ed25519"
ENV_FILE="${KEY_DIR}/env"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root on host (use CI chroot)" >&2
    exit 1
  fi
}

install_direct() {
  section "Install direct sshd port ${TUNNEL_PORT}"
  if [ -z "${CLOUD_AGENT_CIDR}" ]; then
    echo "CLOUD_AGENT_CIDR is required for direct mode (e.g. 3.23.49.121/32)" >&2
    exit 1
  fi
  mkdir -p /etc/ssh/sshd_config.d
  cat > "${SSHD_DROPIN}" <<EOF
# Managed by gitea-diagnostics/open-cloud-agent-tunnel.sh (direct mode)
Port ${TUNNEL_PORT}
PubkeyAuthentication yes
PasswordAuthentication no
EOF
  if command -v sshd >/dev/null 2>&1; then
    sshd -t
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  if command -v iptables >/dev/null 2>&1; then
    if ! iptables -C INPUT -s "${CLOUD_AGENT_CIDR}" -p tcp --dport "${TUNNEL_PORT}" -j ACCEPT 2>/dev/null; then
      iptables -I INPUT 1 -s "${CLOUD_AGENT_CIDR}" -p tcp --dport "${TUNNEL_PORT}" -j ACCEPT
      echo "iptables allow ${CLOUD_AGENT_CIDR} -> :${TUNNEL_PORT}"
    else
      echo "iptables rule already present"
    fi
  else
    echo "WARN: iptables not found; port opened in sshd only" >&2
  fi
  section "Direct mode status"
  ss -tlnp | grep -E ":${TUNNEL_PORT}\\b" || echo "port ${TUNNEL_PORT} not listening yet"
  echo "Cloud Agent: ssh -p ${TUNNEL_PORT} cursor-agent@git.qingxiang.tech"
}

bootstrap_relay() {
  section "Bootstrap relay keypair (one-time)"
  mkdir -p "${KEY_DIR}"
  chmod 700 "${KEY_DIR}"
  if [ ! -f "${KEY_FILE}" ]; then
    ssh-keygen -t ed25519 -f "${KEY_FILE}" -N "" -C "viknow-cloud-agent-tunnel@236"
    chmod 600 "${KEY_FILE}"
  fi
  echo "1) Add the following public key to ${RELAY_USER}@${RELAY_HOST} ~/.ssh/authorized_keys"
  echo "2) Store the private key in Gitea repo secret CLOUD_AGENT_TUNNEL_SSH_KEY"
  echo "3) Re-run workflow with mode=relay action=install"
  echo
  cat "${KEY_FILE}.pub"
  echo
  echo "--- private key (for secret) ---"
  cat "${KEY_FILE}"
}

install_relay() {
  section "Install reverse SSH tunnel to relay"
  if [ -z "${TUNNEL_SSH_PRIVATE_KEY:-}" ]; then
    echo "TUNNEL_SSH_PRIVATE_KEY secret is required for relay mode" >&2
    exit 1
  fi
  mkdir -p "${KEY_DIR}"
  chmod 700 "${KEY_DIR}"
  printf '%s\n' "${TUNNEL_SSH_PRIVATE_KEY}" > "${KEY_FILE}"
  chmod 600 "${KEY_FILE}"
  cat > "${ENV_FILE}" <<EOF
RELAY_HOST=${RELAY_HOST}
RELAY_USER=${RELAY_USER}
REMOTE_PORT=${TUNNEL_PORT}
EOF
  chmod 600 "${ENV_FILE}"

  SSH_BIN="$(command -v autossh || command -v ssh)"
  if [ "$(basename "${SSH_BIN}")" = "autossh" ]; then
    AUTOSSH_ARGS='-M 0'
  else
    AUTOSSH_ARGS=''
  fi

  cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=ViKnow Cloud Agent reverse SSH tunnel (${MODE})
After=network-online.target ssh.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
ExecStart=${SSH_BIN} ${AUTOSSH_ARGS} -N \\
  -o ServerAliveInterval=30 \\
  -o ServerAliveCountMax=3 \\
  -o ExitOnForwardFailure=yes \\
  -o StrictHostKeyChecking=accept-new \\
  -i ${KEY_FILE} \\
  -R 0.0.0.0:\${REMOTE_PORT}:127.0.0.1:22 \\
  \${RELAY_USER}@\${RELAY_HOST}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now viknow-cloud-agent-tunnel.service
  sleep 2
  section "Relay mode status"
  systemctl --no-pager status viknow-cloud-agent-tunnel.service || true
  echo "Cloud Agent: ssh -p ${TUNNEL_PORT} cursor-agent@${RELAY_HOST}"
  echo "Prerequisite on ${RELAY_HOST}: sshd GatewayPorts yes + user ${RELAY_USER} key authorized"
}

show_status() {
  section "Tunnel status (${MODE})"
  if [ -f "${UNIT_FILE}" ]; then
    systemctl --no-pager status viknow-cloud-agent-tunnel.service || true
  fi
  if [ -f "${SSHD_DROPIN}" ]; then
    echo "--- ${SSHD_DROPIN} ---"
    cat "${SSHD_DROPIN}"
  fi
  ss -tlnp | grep -E ":(${TUNNEL_PORT}|22)\\b" || true
  if command -v iptables >/dev/null 2>&1; then
    iptables -S INPUT | grep -E "${TUNNEL_PORT}|${CLOUD_AGENT_CIDR%%/*}" || true
  fi
}

stop_all() {
  section "Stop Cloud Agent tunnel"
  systemctl disable --now viknow-cloud-agent-tunnel.service 2>/dev/null || true
  rm -f "${UNIT_FILE}"
  systemctl daemon-reload
  if [ -f "${SSHD_DROPIN}" ]; then
    rm -f "${SSHD_DROPIN}"
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
  fi
  echo "stopped"
}

require_root
case "${ACTION}" in
  install)
    case "${MODE}" in
      direct) install_direct ;;
      relay) install_relay ;;
      bootstrap) bootstrap_relay ;;
      *) echo "unknown TUNNEL_MODE=${MODE}" >&2; exit 1 ;;
    esac
    show_status
    ;;
  status) show_status ;;
  stop) stop_all ;;
  *)
    echo "unknown TUNNEL_ACTION=${ACTION}" >&2
    exit 1
    ;;
esac

section "Done"
