# 方案 A：经 git.qingxiang.tech:2223 直连 236 host sshd

## 状态（2026-08-19）

| 步骤 | 状态 | Gitea run |
|------|------|-----------|
| 236 上 `cursor-agent` + `sshd :2223` | ✅ 完成 | #602 |
| 网关 `101.71.223.113` 转发 `:2223` → 236 | ⏳ **需人工** | #604 自动配置失败 |
| Cloud Agent 更新 `VIKNOW_236_SSH_KEY` | ⏳ 需人工 | 私钥在 #602 日志 |
| Cloud Agent 连通性验证 | ⏳ 待网关转发后 | — |

## 连接命令

```bash
ssh -p 2223 cursor-agent@git.qingxiang.tech
docker exec -it viknow2-test-dev bash
```

## 236 侧（已由 CI 完成）

Actions → **Setup SSH 2222 for Cloud Agent**（实际端口 `2223`）→ branch `agent/cloud-agent-tunnel`。

- `:2222` 是 socat → Gitea `git` SSH，**不能**用于 `cursor-agent`
- host `sshd` 监听 `:2223`，本机自测 `local_ok`

## 网关侧（一次性，约 1 分钟）

在 **101.71.223.113**（`git.qingxiang.tech` 公网入口）执行：

```bash
# 目标 IP 以 Actions run「Setup gateway 2223 forward」日志为准
TARGET=172.30.57.94   # 若不通，试 36.103.198.236 或内网 IP

sudo apt-get install -y socat
sudo tee /etc/systemd/system/viknow-cursor-agent-ssh-forward.service <<EOF
[Unit]
Description=Forward cursor-agent SSH :2223 to 236
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:2223,fork,reuseaddr,bind=0.0.0.0 TCP:${TARGET}:2223
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now viknow-cursor-agent-ssh-forward
ss -tlnp | grep 2223
```

验证（任意能访问公网机器）：

```bash
ssh -p 2223 -i <cursor-agent-key> cursor-agent@git.qingxiang.tech hostname
# 应输出 36-103-198-236
```

## Cursor Secret

将 run #602 日志中 `CLOUD_AGENT_CURSOR_SSH_KEY` 完整内容写入 Cursor Secret **`VIKNOW_236_SSH_KEY`**（当前跳板 Secret 损坏会导致 `libcrypto` 错误）。

## 相关 workflow（分支 `agent/cloud-agent-tunnel`）

- `setup-ssh-2222.yml` — 配置 236 sshd
- `setup-gateway-2223-forward.yml` — 尝试自动配置网关（目前会因无 root@网关 而失败，日志含手动命令）
- `probe-relay-ssh.yml` — 探测 236 → 网关 SSH 路径
