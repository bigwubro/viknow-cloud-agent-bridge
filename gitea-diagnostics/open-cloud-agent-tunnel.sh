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

RELAY_SSHD_DROPIN="/etc/ssh/sshd_config.d/98-cloud-agent-relay.conf"
CURSOR_AGENT_USER="${CURSOR_AGENT_USER:-cursor-agent}"
CURSOR_KEY_DIR="/etc/viknow/cloud-agent-cursor"
CURSOR_KEY_FILE="${CURSOR_KEY_DIR}/id_ed25519"

section() { printf '\n========== %s ==========\n' "$*"; }

ensure_relay_key() {
  mkdir -p "${KEY_DIR}"
  chmod 700 "${KEY_DIR}"
  if [ ! -f "${KEY_FILE}" ]; then
    ssh-keygen -t ed25519 -f "${KEY_FILE}" -N "" -C "viknow-cloud-agent-tunnel@236"
    chmod 600 "${KEY_FILE}"
  fi
}

setup_relay_host_local() {
  section "Configure local relay host (${RELAY_USER}, GatewayPorts)"
  id "${RELAY_USER}" &>/dev/null || useradd -m -s /bin/bash "${RELAY_USER}"
  install -d -m 700 -o "${RELAY_USER}" -g "${RELAY_USER}" "/home/${RELAY_USER}/.ssh"
  touch "/home/${RELAY_USER}/.ssh/authorized_keys"
  chown "${RELAY_USER}:${RELAY_USER}" "/home/${RELAY_USER}/.ssh/authorized_keys"
  chmod 600 "/home/${RELAY_USER}/.ssh/authorized_keys"
  pub="$(cat "${KEY_FILE}.pub")"
  if ! grep -qF "${pub}" "/home/${RELAY_USER}/.ssh/authorized_keys"; then
    echo "${pub}" >> "/home/${RELAY_USER}/.ssh/authorized_keys"
  fi
  mkdir -p /etc/ssh/sshd_config.d
  cat > "${RELAY_SSHD_DROPIN}" <<'EOF'
# Managed by gitea-diagnostics/open-cloud-agent-tunnel.sh (relay)
GatewayPorts clientspecified
AllowTcpForwarding yes
EOF
  if command -v sshd >/dev/null 2>&1; then
    sshd -t
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  echo "relay user ${RELAY_USER} ready; GatewayPorts enabled"
}

rotate_cursor_agent_key() {
  section "Ensure ${CURSOR_AGENT_USER} SSH key for Cloud Agent"
  id "${CURSOR_AGENT_USER}" &>/dev/null || useradd -m -s /bin/bash "${CURSOR_AGENT_USER}"
  install -d -m 700 -o "${CURSOR_AGENT_USER}" -g "${CURSOR_AGENT_USER}" "/home/${CURSOR_AGENT_USER}/.ssh"
  mkdir -p "${CURSOR_KEY_DIR}"
  chmod 700 "${CURSOR_KEY_DIR}"
  if [ ! -f "${CURSOR_KEY_FILE}" ]; then
    ssh-keygen -t ed25519 -f "${CURSOR_KEY_FILE}" -N "" -C "cursor-agent@236"
    chmod 600 "${CURSOR_KEY_FILE}"
  fi
  pub="$(cat "${CURSOR_KEY_FILE}.pub")"
  auth="/home/${CURSOR_AGENT_USER}/.ssh/authorized_keys"
  touch "${auth}"
  chown "${CURSOR_AGENT_USER}:${CURSOR_AGENT_USER}" "${auth}"
  chmod 600 "${auth}"
  printf '%s\n' "${pub}" > "${auth}"
  echo "cursor-agent authorized_keys updated"
  echo "--- CLOUD_AGENT_CURSOR_SSH_KEY (put in Cursor Secret VIKNOW_236_SSH_KEY) ---"
  cat "${CURSOR_KEY_FILE}"
  echo "--- END CLOUD_AGENT_CURSOR_SSH_KEY ---"
}

install_relay_from_file() {
  TUNNEL_SSH_PRIVATE_KEY="$(cat "${KEY_FILE}")"
  install_relay
}

install_auto() {
  section "Auto setup relay via ${RELAY_HOST}"
  ensure_relay_key
  setup_relay_host_local
  rotate_cursor_agent_key
  if setup_relay_host_remote; then
    install_relay_from_file
  else
    echo "WARN: remote relay host not configured; tunnel service may fail until ${RELAY_USER}@${RELAY_HOST} is set up" >&2
    install_relay_from_file
  fi
  echo "--- CLOUD_AGENT_TUNNEL_SSH_KEY (for Gitea secret, optional) ---"
  cat "${KEY_FILE}"
  echo "--- END CLOUD_AGENT_TUNNEL_SSH_KEY ---"
}

setup_relay_host_remote() {
  section "Try configure remote relay host ${RELAY_USER}@${RELAY_HOST}"
  pub="$(cat "${KEY_FILE}.pub")"
  remote_script=$(cat <<EOS
set -euo pipefail
id ${RELAY_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash ${RELAY_USER}
install -d -m 700 -o ${RELAY_USER} -g ${RELAY_USER} /home/${RELAY_USER}/.ssh
touch /home/${RELAY_USER}/.ssh/authorized_keys
chown ${RELAY_USER}:${RELAY_USER} /home/${RELAY_USER}/.ssh/authorized_keys
chmod 600 /home/${RELAY_USER}/.ssh/authorized_keys
grep -qF '${pub}' /home/${RELAY_USER}/.ssh/authorized_keys || echo '${pub}' >> /home/${RELAY_USER}/.ssh/authorized_keys
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/98-cloud-agent-relay.conf <<'EOF'
GatewayPorts clientspecified
AllowTcpForwarding yes
EOF
sshd -t
systemctl reload ssh || systemctl reload sshd
echo remote_ok
EOS
)
  for spec in "root@10.168.1.233" "root@101.71.223.113" "root@git.qingxiang.tech"; do
    user="${spec%@*}"
    host="${spec#*@}"
    echo "trying ${spec}"
    if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${spec}" "bash -s" <<< "${remote_script}" 2>/dev/null | grep -q remote_ok; then
      echo "remote relay configured via ${spec}"
      return 0
    fi
  done
  echo "no remote SSH path available from 236"
  return 1
}

probe_relay_ssh() {
  section "Probe SSH from 236"
  bash -s <<'EOS'
set -euo pipefail
hostname
ls -la /root/.ssh 2>/dev/null || true
for spec in root@10.168.1.233 root@101.71.223.113 root@git.qingxiang.tech tunnel@git.qingxiang.tech; do
  echo "--- $spec ---"
  ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new "$spec" hostname 2>&1 | head -2 || true
done
EOS
}

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
  ensure_relay_key
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
ExecStart=${SSH_BIN} ${AUTOSSH_ARGS} -N \\
  -o ServerAliveInterval=30 \\
  -o ServerAliveCountMax=3 \\
  -o ExitOnForwardFailure=yes \\
  -o StrictHostKeyChecking=accept-new \\
  -i ${KEY_FILE} \\
  -R 0.0.0.0:${TUNNEL_PORT}:127.0.0.1:22 ${RELAY_USER}@${RELAY_HOST}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  rm -f "${ENV_FILE}"

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
      auto) install_auto ;;
      probe) probe_relay_ssh ;;
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
