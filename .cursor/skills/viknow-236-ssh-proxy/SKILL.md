---
name: viknow-236-ssh-proxy
description: Connect ViKnow Cloud Agents to 236 via Alibaba Cloud SSH forward proxy (ProxyJump). Use when developing on viknow2 in viknow2-test-dev, when AGENTS.md mentions 236 SSH, or when direct access to 36.103.198.236 fails.
environments: [cloud]
---
# ViKnow 236 SSH 正向代理（阿里云跳板）

> **Agent：本文件不会自动加载，你必须用 Read 工具打开并完整遵循。**  
> 若 `36.103.198.236:22` timeout，说明你在直连——立即改用下方 ProxyCommand，不要探测其他端口。

Cloud Agent **不能直连** `36.103.198.236`（机房入站对国外/AWS IP 丢包）。  
必须通过 **阿里云跳板机** 做 SSH 正向代理（`ProxyJump` / `ProxyCommand`）。

```
Cloud Agent ──SSH──► 跳板 8.145.57.129 ──SSH──► 236:22 (cursor-agent) ──► docker exec viknow2-test-dev
```

跳板已配置好到 236 的密钥；Cloud Agent 侧只需两个 Secret + 下列命令。

---

## 必做：Cursor Secrets

| Secret | 内容 | 注意 |
|--------|------|------|
| `VIKNOW_236_SSH_KEY` | `cursor-agent@236` 的 **完整 OpenSSH 私钥** | 写入前 **去掉 `-----BEGIN` 前面的空行**，否则 `libcrypto` 报错 |
| `VIKNOW_BASTION_SSH_PASSWORD` | 跳板 `root@8.145.57.129` 的登录密码 | **不要写进仓库** |

公钥指纹（仅用于核对，不是 Secret）：`SHA256:Gfour1wHjbzi…`（与 run #602 `cursor-agent@236` 一致）

---

## 第一步：落地密钥

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$VIKNOW_236_SSH_KEY" | sed '/./,$!d' > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh-keygen -y -f ~/.ssh/id_ed25519   # 应能输出公钥，否则 Secret 格式错误
```

需要密码跳板时安装 `sshpass`（若无）：

```bash
command -v sshpass >/dev/null || sudo apt-get install -y sshpass
```

---

## 第二步：连通性冒烟（必须通过再开发）

**不要**直连 `36.103.198.236`、**不要**用 `bore.pub`、**不要**测 236 的 2223/42236。

```bash
ssh -i ~/.ssh/id_ed25519 \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=25 \
  -o ProxyCommand="sshpass -p \"$VIKNOW_BASTION_SSH_PASSWORD\" ssh -o StrictHostKeyChecking=no -W %h:%p root@8.145.57.129" \
  -p 22 cursor-agent@36.103.198.236 \
  'echo ok && hostname && whoami'
```

**期望输出：**

```
ok
36-103-198-236
cursor-agent
```

---

## 第三步：进入开发容器

业务代码在 **`viknow2-test-dev`** 容器内，工作目录多为 **`/opt/viknow`**（不是 `/workspace`）。

```bash
ssh -i ~/.ssh/id_ed25519 \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=30 \
  -o ProxyCommand="sshpass -p \"$VIKNOW_BASTION_SSH_PASSWORD\" ssh -o StrictHostKeyChecking=no -W %h:%p root@8.145.57.129" \
  -p 22 cursor-agent@36.103.198.236 \
  'docker exec viknow2-test-dev bash -lc "hostname && pwd"'
```

交互式：

```bash
ssh ... cursor-agent@36.103.198.236
docker exec -it viknow2-test-dev bash
cd /opt/viknow
```

---

## 推荐：`~/.ssh/config`（减少重复参数）

在 Cloud Agent 环境写入：

```
Host viknow-bastion
  HostName 8.145.57.129
  User root
  StrictHostKeyChecking accept-new

Host viknow-236
  HostName 36.103.198.236
  User cursor-agent
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  ProxyCommand sshpass -p "ENV_BASTION_PASS" ssh -o StrictHostKeyChecking=no -W %h:%p viknow-bastion
```

将 `ENV_BASTION_PASS` 替换为环境变量展开，或生成 config 时注入 `$VIKNOW_BASTION_SSH_PASSWORD`：

```bash
cat > ~/.ssh/config <<EOF
Host viknow-bastion
  HostName 8.145.57.129
  User root
  StrictHostKeyChecking accept-new

Host viknow-236
  HostName 36.103.198.236
  User cursor-agent
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  ProxyCommand sshpass -p "$VIKNOW_BASTION_SSH_PASSWORD" ssh -o StrictHostKeyChecking=no -W %h:%p viknow-bastion
EOF
chmod 600 ~/.ssh/config
ssh viknow-236 'echo ok && hostname && whoami'
```

---

## 在 236 / 容器内开发（Gitea viknow2）

```bash
ssh viknow-236 'docker exec viknow2-test-dev bash -lc "
  cd /opt/viknow &&
  git fetch origin &&
  git checkout -B agent/<topic> origin/main &&
  # ... 改代码、测试 ...
  git push -u origin agent/<topic>
"'
```

- 业务仓：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- **禁止** `git push` 到 `main`
- **禁止** 改、停、删 `viknow2-test` / private 相关容器

---

## 完整冒烟（可选）

```bash
ssh viknow-236 'docker exec viknow2-test-dev bash -lc "
  cd /opt/viknow &&
  git fetch origin &&
  git checkout -B agent/cloud-smoke origin/main &&
  git commit --allow-empty -m \"chore: cloud agent smoke\" &&
  git push -u origin agent/cloud-smoke &&
  git log -1 --oneline &&
  git status -sb
"'
```

成功标志：Gitea `castmeta-research/viknow2` 出现分支 `agent/cloud-smoke`。

---

## 禁止事项

- **不要**直连 `36.103.198.236`（任意端口 22/2223/42236）
- **不要**使用 `bore.pub` 或其它反向隧道地址（已弃用，统一走跳板）
- **不要**在 236 上生成新 SSH 密钥或改 `authorized_keys`
- **不要把**跳板密码、236 私钥写入 git 提交
- **不要**在本跳板仓实现 ViKnow 业务功能

---

## 故障排查

| 现象 | 处理 |
|------|------|
| `error in libcrypto` | Secret `VIKNOW_236_SSH_KEY` 含多余空行；用 `sed '/./,$!d'` 去掉 `BEGIN` 前空行 |
| `Connection timed out` 连 236 | 正常：说明在直连。必须加 `ProxyCommand` 走 `8.145.57.129` |
| `Permission denied` 连 236 | 只核对 Secret 私钥是否与 236 上 `cursor-agent` 公钥匹配；**不要**改 236 `authorized_keys` |
| `Permission denied` 连跳板 | 检查 `VIKNOW_BASTION_SSH_PASSWORD`；或请管理员为 Cloud Agent 公钥开通跳板密钥登录 |
| 跳板通、236 不通 | 在跳板上测：`ssh -i /root/.ssh/id_ed25519_cursor cursor-agent@36.103.198.236`（需管理员） |
| `docker exec` 失败 | `docker ps` 确认 `viknow2-test-dev` 在运行 |

---

## 固定参数速查

| 项 | 值 |
|----|-----|
| 跳板公网 IP | `8.145.57.129` |
| 跳板用户 | `root` |
| 236 地址 | `36.103.198.236` |
| 236 SSH 用户 | `cursor-agent` |
| 236 SSH 端口 | `22`（经跳板，非直连） |
| 开发容器 | `viknow2-test-dev` |
| 容器内工作目录 | `/opt/viknow` |

---

## 架构说明（给维护者）

- **正向代理**：Cloud Agent → 跳板（公网可达）→ 跳板主动连 236（同运营商/内网可达）
- 236 **机房入站**仍拦国外 IP；跳板在国内阿里云，可访问 236
- 跳板机已部署 `/root/.ssh/id_ed25519_cursor` 用于跳板→236；与 Cloud Agent 使用的 `VIKNOW_236_SSH_KEY` 为同一把 `cursor-agent` 密钥
