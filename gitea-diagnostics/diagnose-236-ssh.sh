#!/usr/bin/env bash
# Diagnose why external clients (e.g. Cursor Cloud Agent) cannot SSH to this host.
# Intended to run ON 236 via Gitea act runner — no inbound SSH required.
set -euo pipefail

SSH_PORT="${DIAG_SSH_PORT:-22}"
TARGET_USER="${DIAG_SSH_USER:-cursor-agent}"

section() { printf '\n========== %s ==========\n' "$*"; }

section "Host identity"
hostname
date -Is || date
uname -a

section "Listening SSH port"
if command -v ss >/dev/null 2>&1; then
  ss -tlnp | grep -E ":${SSH_PORT}\\b" || echo "port ${SSH_PORT} not listening"
else
  netstat -tlnp 2>/dev/null | grep -E ":${SSH_PORT}\\b" || echo "ss/netstat unavailable"
fi

section "sshd service"
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || true
  systemctl status ssh --no-pager -n 20 2>/dev/null || systemctl status sshd --no-pager -n 20 2>/dev/null || true
else
  echo "systemctl not available (maybe container runner only)"
fi

section "sshd config (effective highlights)"
if [ -r /etc/ssh/sshd_config ]; then
  grep -En '^(Port|ListenAddress|PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|MaxStartups|MaxSessions|AllowUsers|DenyUsers|UsePAM|Subsystem)\b' /etc/ssh/sshd_config || true
else
  echo "/etc/ssh/sshd_config not readable from this context"
fi

section "authorized_keys for ${TARGET_USER}"
ak="/home/${TARGET_USER}/.ssh/authorized_keys"
if [ -r "${ak}" ]; then
  echo "file=${ak} lines=$(wc -l < "${ak}")"
  # show fingerprints only, not full keys
  while read -r line; do
    [ -z "$line" ] && continue
    echo "$line" | ssh-keygen -lf - 2>/dev/null || echo "(unparsed line)"
  done < "${ak}"
else
  echo "missing or unreadable: ${ak}"
  id "${TARGET_USER}" 2>/dev/null || echo "user ${TARGET_USER} not found"
fi

section "Recent SSH auth log (last 80 lines)"
for f in /var/log/auth.log /var/log/secure; do
  if [ -r "$f" ]; then
    echo "--- $f ---"
    tail -n 80 "$f" | grep -Ei 'sshd|accepted|failed|invalid|reset|refused|banner|maxstartups|connection closed' || tail -n 80 "$f"
    break
  fi
done

section "fail2ban / firewall"
if command -v fail2ban-client >/dev/null 2>&1; then
  fail2ban-client status 2>/dev/null || true
  fail2ban-client status sshd 2>/dev/null || true
fi
if command -v ufw >/dev/null 2>&1; then
  ufw status 2>/dev/null || true
fi
if command -v iptables >/dev/null 2>&1; then
  iptables -S 2>/dev/null | head -40 || true
fi

section "Resource pressure (can cause ssh reset)"
uptime || true
free -h 2>/dev/null || true
df -h / /var/log 2>/dev/null || true

section "Local SSH self-test to 127.0.0.1:${SSH_PORT}"
if command -v nc >/dev/null 2>&1; then
  nc -zv 127.0.0.1 "${SSH_PORT}" 2>&1 || true
elif command -v bash >/dev/null 2>&1; then
  timeout 3 bash -c "echo > /dev/tcp/127.0.0.1/${SSH_PORT}" && echo "tcp open" || echo "tcp closed"
fi

section "Cloud Agent expected key fingerprint (reference)"
echo "VIKNOW_236_SSH_KEY public comment: cursor-agent@236"
echo "Expected SHA256: Gfour1wHjbziInpybJT8nGnkB6lfY3A3poVB3loR0O8 (ED25519)"
echo "Compare with authorized_keys fingerprints above."

section "Done"
