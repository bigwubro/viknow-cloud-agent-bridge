# 236 Docker 审计 — 安装记录

> 维护说明：安装/变更 auditd 规则后更新本文件。

## 已安装（2026-09-11）

| 项 | 值 |
|---|---|
| 主机 | `36.103.198.236` |
| 安装时间 | 2026-09-11 ~15:37 CST（root / wuzihan） |
| 安装命令 | `sudo bash /home/cursor-agent/viknow-cloud-agent-bridge/docs/ops/236-docker-audit/install-auditd.sh` |
| auditd | **已安装并 active** |
| Docker | **未改配置、未 restart**（needrestart 无 docker.service） |
| 规则 key | `viknow_docker_exec`, `viknow_docker_sock`, `viknow_vllm_script` |

### 236 上脚本绝对路径

```
/home/cursor-agent/viknow-cloud-agent-bridge/docs/ops/236-docker-audit/install-auditd.sh
/home/cursor-agent/viknow-cloud-agent-bridge/docs/ops/236-docker-audit/audit-rules.d/viknow-docker.rules
/home/cursor-agent/viknow-cloud-agent-bridge/docs/ops/236-docker-audit/docker-event-watcher.sh
```

跳板仓（Cloud Agent VM）同源路径：`/workspace/docs/ops/236-docker-audit/`

### 旁路日志（cursor-agent，只读 docker events）

| 项 | 路径 |
|---|---|
| systemd user service | `viknow-docker-event-watcher.service` |
| 日志 | `/home/cursor-agent/.local/log/viknow-docker-events.log` |

**局限**：events 旁路无 UID；归因靠 auditd。

## 事后查询（root）

```bash
auditctl -l | grep viknow

# CLI：docker rm / stop / kill 等
ausearch -k viknow_docker_exec -ts today --interpret

# API 直连 docker.sock（含 DELETE /containers/...）
ausearch -k viknow_docker_sock -ts today --interpret

# jcy 运维脚本
ausearch -k viknow_vllm_script -ts today --interpret

# 与 journal 交叉
journalctl -u docker --since "1 hour ago" | grep -E 'DELETE|vllm-vlm'
grep vllm-vlm /home/cursor-agent/.local/log/viknow-docker-events.log
```

## 历史 incident（auditd 安装前，无法 retroactive 归因）

- **2026-09-11 13:56:42 CST**：`DELETE /containers/vllm-vlm`，`vllm-vlm` exit 137
- journal 有 DELETE；auth.log 无（docker 组直连 socket）
- **自 2026-09-11 auditd 安装后起**，同类操作可记录 Unix uid/命令行

## 待办（可选）

- [ ] sudoers：允许 `cursor-agent` 只读 `ausearch -k viknow_docker_*`（见 README）
- [ ] `loginctl enable-linger cursor-agent`（保证 logout 后 events watcher 仍运行）
