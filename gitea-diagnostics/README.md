# 诊断 Cloud Agent 无法 SSH 登录 236

**目的**：从 **236 本机**（Gitea act runner）检查 sshd / 防火墙 / `authorized_keys`，定位外部 `cursor-agent@36.103.198.236` 握手失败原因。  
**不需要** Cloud Agent 能 SSH 进来才能跑。

## 部署到 Gitea（viknow2 仓）

1. 提交 `gitea-diagnostics/diagnose-236-ssh.sh`
2. 提交 `.gitea/workflows/diagnose-236-ssh.yml`（内容见 `workflow-diagnose-236-ssh.yml`）
3. Gitea → **Actions** → **Diagnose 236 SSH** → **Run workflow**

## 脚本检查项

- `:22` 是否在监听
- `sshd` 服务状态与关键配置（`MaxStartups` 等）
- `cursor-agent` 的 `authorized_keys` 指纹（不打印完整私钥）
- `/var/log/auth.log` 最近 SSH 失败/reset 记录
- fail2ban / ufw / iptables
- 磁盘/内存是否异常

## 说明

- 本目录在 **跳板仓** 仅作模板；**必须在 Gitea `viknow2` 仓触发** 才会在 236 runner 上执行。
- Cloud Agent 侧无 Gitea 写仓 Token 时，需人工在 Gitea 网页提交上述两个文件。

## Cloud Agent 连 236（公网域名）

**不要直连 `36.103.198.236`**（Cloud Agent 会被 reset）。用域名 `git.qingxiang.tech`（已验证可达）。

### 方案 A：relay（经 Gitea 域名中转，无需 Cloudflare）

1. Gitea 主机配置 `tunnel` 用户 + `GatewayPorts yes`
2. Secret `CLOUD_AGENT_TUNNEL_SSH_KEY`
3. Actions → **Open Cloud Agent tunnel** → `mode=relay`
4. 连接：`ssh -p 42236 cursor-agent@git.qingxiang.tech`

### 方案 B：Cloudflare Tunnel（推荐，真正独立域名）

1. Cloudflare Zero Trust 创建 Tunnel，路由如 `ssh-236.qingxiang.tech` → `tcp://localhost:22`
2. Secret `CLOUDFLARE_TUNNEL_TOKEN`
3. Actions → **Setup Cloudflare tunnel for 236** → install
4. 连接：`ssh cursor-agent@ssh-236.qingxiang.tech`

两种方案均需修好 Cursor Secret `VIKNOW_236_SSH_KEY`（cursor-agent 私钥）。

