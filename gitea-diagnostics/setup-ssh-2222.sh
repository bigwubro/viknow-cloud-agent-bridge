#!/usr/bin/env bash
# Fix cursor-agent SSH via git.qingxiang.tech (host sshd on dedicated port, not :2222 socat/git).
set -euo pipefail

ACTION="${SSH2222_ACTION:-install}"
SSH_PORT="${SSH2222_PORT:-2223}"
CURSOR_USER="${CURSOR_AGENT_USER:-cursor-agent}"
KEY_DIR="/etc/viknow/cloud-agent-cursor"
KEY_FILE="${KEY_DIR}/id_ed25519"
DROPIN="/etc/ssh/sshd_config.d/99-cursor-agent-${SSH_PORT}.conf"
LEGACY_DROPIN="/etc/ssh/sshd_config.d/99-cursor-agent-2222.conf"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

ensure_cursor_key() {
  section "Ensure ${CURSOR_USER} key"
  id "${CURSOR_USER}" &>/dev/null || useradd -m -s /bin/bash "${CURSOR_USER}"
  install -d -m 700 -o "${CURSOR_USER}" -g "${CURSOR_USER}" "/home/${CURSOR_USER}/.ssh"
  mkdir -p "${KEY_DIR}"
  chmod 700 "${KEY_DIR}"
  if [ ! -f "${KEY_FILE}" ]; then
    ssh-keygen -t ed25519 -f "${KEY_FILE}" -N "" -C "cursor-agent@236"
    chmod 600 "${KEY_FILE}"
  fi
  pub="$(cat "${KEY_FILE}.pub")"
  auth="/home/${CURSOR_USER}/.ssh/authorized_keys"
  printf '%s\n' "${pub}" > "${auth}"
  chown "${CURSOR_USER}:${CURSOR_USER}" "${auth}"
  chmod 600 "${auth}"
  ssh-keygen -lf "${auth}"
  echo "--- CLOUD_AGENT_CURSOR_SSH_KEY (Cursor Secret VIKNOW_236_SSH_KEY) ---"
  cat "${KEY_FILE}"
  echo "--- END CLOUD_AGENT_CURSOR_SSH_KEY ---"
}

configure_sshd_2222() {
  section "Configure sshd for port ${SSH_PORT}"
  # :2222 is socat -> Gitea git SSH; use a dedicated host sshd port instead.
  rm -f "${LEGACY_DROPIN}"
  mkdir -p /etc/ssh/sshd_config.d
  if ss -tlnp | grep -qE ":${SSH_PORT}\\b"; then
    echo "port ${SSH_PORT} already listening"
    ss -tlnp | grep -E ":${SSH_PORT}\\b" || true
  fi
  cat > "${DROPIN}" <<EOF
# Managed by gitea-diagnostics/setup-ssh-2222.sh
Port ${SSH_PORT}
PubkeyAuthentication yes
PasswordAuthentication no
EOF
  if command -v sshd >/dev/null 2>&1; then
    sshd -t
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  echo "sshd reloaded"
}

show_status() {
  section "Status"
  hostname
  section "Listeners on ${SSH_PORT}"
  ss -tlnp | grep -E ":${SSH_PORT}\\b" || echo "not listening"
  section "sshd config grep ${SSH_PORT}"
  grep -RIn "${SSH_PORT}" /etc/ssh/ 2>/dev/null | head -20 || true
  if [ -f "${DROPIN}" ]; then
    echo "--- ${DROPIN} ---"
    cat "${DROPIN}"
  fi
  auth="/home/${CURSOR_USER}/.ssh/authorized_keys"
  if [ -f "${auth}" ]; then
    echo "--- ${auth} ---"
    ssh-keygen -lf "${auth}" || true
  else
    echo "missing ${auth}"
  fi
  section "Local self-test"
  if [ -f "${KEY_FILE}" ]; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p "${SSH_PORT}" -i "${KEY_FILE}" "${CURSOR_USER}@127.0.0.1" 'echo local_ok' 2>&1 || true
  fi
  echo "Cloud Agent: ssh -p ${SSH_PORT} ${CURSOR_USER}@git.qingxiang.tech"
}

install_all() {
  ensure_cursor_key
  configure_sshd_2222
  show_status
}

require_root
case "${ACTION}" in
  install) install_all ;;
  status) show_status ;;
  *)
    echo "unknown SSH2222_ACTION=${ACTION}" >&2
    exit 1
    ;;
esac
section "Done"
