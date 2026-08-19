#!/usr/bin/env bash
# Harden 236:22 for Cloud Agent: reduce scanner impact + whitelist current egress IP.
set -euo pipefail

ACTION="${FIX22_ACTION:-install}"
CLOUD_AGENT_CIDR="${CLOUD_AGENT_CIDR:-}"
SSH_PORT="${FIX22_PORT:-22}"
DROPIN="/etc/ssh/sshd_config.d/99-cloud-agent-port22.conf"
CHAIN="VIKNOW_SSH22_RATELIMIT"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

configure_sshd() {
  section "Tune sshd for port ${SSH_PORT}"
  mkdir -p /etc/ssh/sshd_config.d
  cat > "${DROPIN}" <<'EOF'
# Managed by gitea-diagnostics/fix-236-port22.sh
# Survive SSH scan storms; Cloud Agent uses pubkey-only cursor-agent.
MaxStartups 120:60:300
LoginGraceTime 20
PasswordAuthentication no
PubkeyAuthentication yes
UseDNS no
EOF
  if command -v sshd >/dev/null 2>&1; then
    sshd -t
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  echo "sshd reloaded (${DROPIN})"
}

configure_iptables() {
  section "iptables for :${SSH_PORT}"
  if ! command -v iptables >/dev/null 2>&1; then
    echo "WARN: iptables not available" >&2
    return 0
  fi

  if [ -n "${CLOUD_AGENT_CIDR}" ]; then
    if ! iptables -C INPUT -s "${CLOUD_AGENT_CIDR}" -p tcp --dport "${SSH_PORT}" -j ACCEPT 2>/dev/null; then
      iptables -I INPUT 1 -s "${CLOUD_AGENT_CIDR}" -p tcp --dport "${SSH_PORT}" -j ACCEPT
      echo "whitelist ${CLOUD_AGENT_CIDR} -> :${SSH_PORT}"
    else
      echo "whitelist already present for ${CLOUD_AGENT_CIDR}"
    fi
  else
    echo "WARN: CLOUD_AGENT_CIDR not set; skipping IP whitelist" >&2
  fi

  # Rate-limit new SSH handshakes per source (scanners hit this; whitelist above wins first).
  if ! iptables -nL "${CHAIN}" >/dev/null 2>&1; then
    iptables -N "${CHAIN}"
  fi
  iptables -F "${CHAIN}"
  iptables -A "${CHAIN}" -m recent --name VIKNOW_SSH22 --rcheck --seconds 60 --hitcount 8 -j DROP
  iptables -A "${CHAIN}" -m recent --name VIKNOW_SSH22 --set
  iptables -A "${CHAIN}" -j ACCEPT
  if ! iptables -C INPUT -p tcp --dport "${SSH_PORT}" -m state --state NEW -j "${CHAIN}" 2>/dev/null; then
    iptables -I INPUT 2 -p tcp --dport "${SSH_PORT}" -m state --state NEW -j "${CHAIN}"
    echo "rate-limit chain ${CHAIN} installed on :${SSH_PORT}"
  fi

  echo "--- INPUT rules (top 15) ---"
  iptables -S INPUT | head -15
}

show_status() {
  section "sshd listeners"
  ss -tlnp | grep -E ":${SSH_PORT}\\b" || true
  section "MaxStartups / recent drops"
  grep -E 'MaxStartups|drop connection' /var/log/auth.log 2>/dev/null | tail -10 || true
  section "cursor-agent authorized_keys"
  ak="/home/cursor-agent/.ssh/authorized_keys"
  if [ -f "${ak}" ]; then
    ssh-keygen -lf "${ak}" || true
  else
    echo "missing ${ak}"
  fi
  section "Local self-test :${SSH_PORT}"
  key="/etc/viknow/cloud-agent-cursor/id_ed25519"
  if [ -f "${key}" ]; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p "${SSH_PORT}" -i "${key}" cursor-agent@127.0.0.1 'echo local_ok' 2>&1 || true
  fi
  if [ -n "${CLOUD_AGENT_CIDR}" ]; then
    ip="${CLOUD_AGENT_CIDR%/32}"
    echo "Cloud Agent: ssh cursor-agent@${ip%%/*}  (or @36.103.198.236 if whitelisted ${CLOUD_AGENT_CIDR})"
  fi
  echo "Cloud Agent: ssh cursor-agent@36.103.198.236"
}

install_all() {
  configure_sshd
  configure_iptables
  show_status
}

require_root
case "${ACTION}" in
  install) install_all ;;
  status) show_status ;;
  *)
    echo "unknown FIX22_ACTION=${ACTION}" >&2
    exit 1
    ;;
esac
section "Done"
