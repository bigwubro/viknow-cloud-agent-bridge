#!/usr/bin/env bash
# 236 主机一次性安装 Docker 审计（需 root）
# 用法: sudo bash install-auditd.sh [--with-system-watcher]
#
# 安全保证：不修改 /etc/docker/*、不 restart docker、不改 docker.sock 权限或 docker 组。
# 仅安装 auditd 规则（watch docker 二进制与 socket）及可选的 docker events 旁路日志。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_SYSTEM_WATCHER=0

for arg in "$@"; do
  case "$arg" in
    --with-system-watcher) WITH_SYSTEM_WATCHER=1 ;;
    --with-guard)
      echo "DEPRECATED: --with-guard removed (use auditd only; do not wrap docker binary)." >&2
      exit 2
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y auditd audispd-plugins

install -d /etc/audit/rules.d
install -m 0640 "${SCRIPT_DIR}/audit-rules.d/viknow-docker.rules" \
  /etc/audit/rules.d/viknow-docker.rules

# 合并并加载规则
augenrules --load
auditctl -l | grep -E 'viknow_docker|viknow_vllm' || {
  echo "WARN: audit rules not loaded; check /etc/audit/rules.d/" >&2
}

systemctl enable --now auditd
systemctl restart auditd

# 可选：系统级 docker events 旁路（与 auditd 互补；只读订阅，不改 Docker 配置）
if [[ "$WITH_SYSTEM_WATCHER" -eq 1 ]]; then
  install -d /var/log/viknow /opt/viknow/bin
  install -m 0755 "${SCRIPT_DIR}/docker-event-watcher.sh" /opt/viknow/bin/docker-event-watcher.sh
  install -m 0644 "${SCRIPT_DIR}/docker-event-watcher.service" /etc/systemd/system/viknow-docker-event-watcher.service
  sed -i 's|%i|viknow-docker-events|' "${SCRIPT_DIR}/docker-event-watcher.service" 2>/dev/null || true
  # 使用系统 service 模板
  cat > /etc/systemd/system/viknow-docker-event-watcher.service << UNIT
[Unit]
Description=ViKnow docker events audit watcher
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/opt/viknow/bin/docker-event-watcher.sh /var/log/viknow/docker-events.log
Restart=always
RestartSec=5
User=root
Group=docker

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now viknow-docker-event-watcher.service
fi

cat << EOF

auditd installed. Quick verify:
  auditctl -l | grep viknow
  docker run --rm --name audit-smoke-test alpine:true 2>/dev/null || true
  ausearch -k viknow_docker_exec -ts recent --interpret | tail -5

Query later:
  ausearch -k viknow_docker_sock -ts today --interpret
  ausearch -k viknow_docker_exec -ts today --interpret

Optional sudoers for cursor-agent forensics:
  cursor-agent ALL=(ALL) NOPASSWD: /usr/sbin/ausearch -k viknow_docker_exec *
  cursor-agent ALL=(ALL) NOPASSWD: /usr/sbin/ausearch -k viknow_docker_sock *
EOF
