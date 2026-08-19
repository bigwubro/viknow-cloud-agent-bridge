#!/usr/bin/env bash
# Revert fix-236-port22.sh changes so existing :22 SSH is unaffected.
set -euo pipefail

ACTION="${REVERT22_ACTION:-revert}"
DROPIN="/etc/ssh/sshd_config.d/99-cloud-agent-port22.conf"
CHAIN="VIKNOW_SSH22_RATELIMIT"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

revert_sshd() {
  section "Remove port22 hardening drop-in"
  if [ -f "${DROPIN}" ]; then
    rm -f "${DROPIN}"
    echo "removed ${DROPIN}"
    if command -v sshd >/dev/null 2>&1; then
      sshd -t
    fi
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    echo "sshd reloaded"
  else
    echo "drop-in not present"
  fi
}

revert_iptables() {
  section "Remove Cloud Agent :22 iptables rules"
  if ! command -v iptables >/dev/null 2>&1; then
    echo "iptables not available"
    return 0
  fi
  while iptables -C INPUT -p tcp --dport 22 -m state --state NEW -j "${CHAIN}" 2>/dev/null; do
    iptables -D INPUT -p tcp --dport 22 -m state --state NEW -j "${CHAIN}"
    echo "removed jump to ${CHAIN}"
  done
  if iptables -nL "${CHAIN}" >/dev/null 2>&1; then
    iptables -F "${CHAIN}"
    iptables -X "${CHAIN}"
    echo "removed chain ${CHAIN}"
  fi
  while read -r rule; do
    [ -z "${rule}" ] && continue
    src="$(echo "${rule}" | awk '{for(i=1;i<=NF;i++) if($i=="-s") print $(i+1)}')"
    [ -n "${src}" ] || continue
    while iptables -D INPUT -s "${src}" -p tcp --dport 22 -j ACCEPT 2>/dev/null; do
      echo "removed whitelist ${src}"
    done
  done < <(iptables -S INPUT 2>/dev/null | grep 'dport 22' | grep '/32' || true)
  echo "--- INPUT top rules after revert ---"
  iptables -S INPUT | head -15 || true
}

show_status() {
  section "sshd on :22"
  ss -tlnp | grep -E ':22\b' || echo "port 22 not listening"
  section "remaining port22 drop-ins"
  ls -la /etc/ssh/sshd_config.d/*port22* 2>/dev/null || echo "(none)"
}

revert_all() {
  revert_sshd
  revert_iptables
  show_status
  echo "Existing :22 SSH should behave as before fix-236-port22"
}

require_root
case "${ACTION}" in
  revert) revert_all ;;
  status) show_status ;;
  *)
    echo "unknown REVERT22_ACTION=${ACTION}" >&2
    exit 1
    ;;
esac
section "Done"
