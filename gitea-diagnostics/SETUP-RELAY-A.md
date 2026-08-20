# 方案 A：经 git.qingxiang.tech 连 236

Cloud Agent **不要**连 `36.103.198.236`，改连域名：

```bash
ssh -p 42236 cursor-agent@git.qingxiang.tech
```

## 架构

```
Cloud Agent  →  git.qingxiang.tech:42236  →  (反向隧道)  →  236:22
```

## 一次性配置（4 步）

### 步骤 1：Gitea 主机上创建 tunnel 用户

在 **git.qingxiang.tech 那台机器**（公网 `101.71.223.113`）上执行：

```bash
sudo useradd -m -s /bin/bash tunnel 2>/dev/null || true
sudo mkdir -p /home/tunnel/.ssh
sudo chmod 700 /home/tunnel/.ssh
```

编辑 `/etc/ssh/sshd_config` 或 drop-in，确保：

```
GatewayPorts yes
AllowTcpForwarding yes
```

然后：

```bash
sudo systemctl reload sshd
```

### 步骤 2：Gitea Actions 生成密钥对

Gitea → `castmeta-research/viknow2` → **Actions** → **Open Cloud Agent tunnel** → Run workflow：

| 参数 | 值 |
|------|-----|
| branch | `agent/cloud-agent-tunnel` |
| mode | `bootstrap` |
| action | `install` |

打开本次 run 的日志，找到 **公钥** 和 **私钥** 两段。

### 步骤 3：公钥上机 + 私钥进 Secret

**Gitea 主机上**（步骤 1 同一台）：

```bash
# 粘贴 bootstrap 日志里的公钥（单行）
echo 'ssh-ed25519 AAAA... viknow-cloud-agent-tunnel@236' | sudo tee -a /home/tunnel/.ssh/authorized_keys
sudo chmod 600 /home/tunnel/.ssh/authorized_keys
sudo chown -R tunnel:tunnel /home/tunnel/.ssh
```

**Gitea 仓库 Settings → Secrets** 新增：

| Secret 名 | 值 |
|-----------|-----|
| `CLOUD_AGENT_TUNNEL_SSH_KEY` | bootstrap 日志里的**完整私钥**（含 BEGIN/END 行） |

### 步骤 4：启动反向隧道

再跑一次 **Open Cloud Agent tunnel**：

| 参数 | 值 |
|------|-----|
| mode | `relay` |
| action | `install` |
| tunnel_port | `42236` |

日志里应出现 `active (running)` 和 `Cloud Agent: ssh -p 42236 cursor-agent@git.qingxiang.tech`。

---

## Cloud Agent 侧

1. 修好 Cursor Secret **`VIKNOW_236_SSH_KEY`**（完整 `cursor-agent` 私钥，当前是坏的 `libcrypto` 错误）
2. 连接：

```bash
ssh -p 42236 cursor-agent@git.qingxiang.tech
docker exec -it viknow2-test-dev bash
```

## 排查

```bash
# Gitea 主机上
sudo ss -tlnp | grep 42236          # 应看到 sshd 在监听 42236
sudo systemctl status sshd

# 236 上
sudo systemctl status viknow-cloud-agent-tunnel.service
```

停止隧道：workflow `action=stop`。

## 文件位置（viknow2 分支 agent/cloud-agent-tunnel）

- `gitea-diagnostics/open-cloud-agent-tunnel.sh`
- `.gitea/workflows/open-cloud-agent-tunnel.yml`

若 Actions 里看不到 workflow，需先把该分支 merge 进 `main`，或从分支页触发 dispatch。
