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

## Cloud Agent 入口隧道（`open-cloud-agent-tunnel`）

Cloud Agent 直连 `36.103.198.236:22` 可能被上游网络 reset。推荐 **relay 模式**：

1. 在 Gitea 主机 `101.71.223.113` 创建用户 `tunnel`，配置 `sshd`：`GatewayPorts yes`、`AllowTcpForwarding yes`
2. 在 Gitea 仓 Secrets 添加 `CLOUD_AGENT_TUNNEL_SSH_KEY`（`tunnel` 用户私钥）
3. Actions → **Open Cloud Agent tunnel** → Run workflow
   - `mode`: `relay`（推荐）或 `direct`（在 236 开放端口 + iptables 白名单）
   - `cloud_agent_ip`: direct 模式必填（当前 Cloud Agent 出口 IP）
4. Cloud Agent 连接：
   - relay: `ssh -p 42236 cursor-agent@101.71.223.113`
   - direct: `ssh -p 42236 cursor-agent@36.103.198.236`

文件：`gitea-diagnostics/open-cloud-agent-tunnel.sh`、`.gitea/workflows/open-cloud-agent-tunnel.yml`
