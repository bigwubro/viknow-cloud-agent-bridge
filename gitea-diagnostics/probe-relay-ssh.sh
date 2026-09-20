#!/usr/bin/env bash
# Probe SSH paths from 236 to Gitea relay host candidates.
set -euo pipefail
section() { printf '\n========== %s ==========\n' "$*"; }
section "Host identity"
hostname
section "root ssh keys"
ls -la /root/.ssh 2>/dev/null || echo "no /root/.ssh"
section "probe hosts"
for spec in \
  "root@10.168.1.233" \
  "root@101.71.223.113" \
  "root@git.qingxiang.tech" \
  "tunnel@git.qingxiang.tech" \
  "git@git.qingxiang.tech:2222"; do
  userhost="${spec%%:*}"
  port=22
  if [[ "$spec" == *:* ]]; then
    userhost="${spec%:*}"
    port="${spec##*:}"
  fi
  user="${userhost%@*}"
  host="${userhost#*@}"
  echo "--- ${user}@${host}:${port} ---"
  ssh -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new -p "$port" "${user}@${host}" 'hostname' 2>&1 | head -2 || true
done
section "Done"
