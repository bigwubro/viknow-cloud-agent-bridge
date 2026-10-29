# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
- API / 数据模型文档站（实现以它为准）：<http://36.103.198.236:5208/>，对话接口 <http://36.103.198.236:5208/start/api/runs.html>，会话数据模型 <http://36.103.198.236:5208/model/session.html>
- Cloud Agent 在本仓库启动后，必须 SSH 到 236 再 `docker exec` 进入开发容器

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600）
2. **236 长连接（推荐）**：先起 SSH 复用主连接，后续命令不再每次 TCP/握手（Cloud Agent 同一次运行内有效）：

```bash
bash scripts/viknow236-ssh-master.sh   # 后台 ssh -fN，ControlMaster 保活 8h
ssh 236 'echo ok'                      # 走 ~/.ssh/cm-* 复用
ssh 236 'docker exec viknow2-test-dev bash -lc "..."'
```

`~/.ssh/config` 里应对 `36.103.198.236` 配置 `ControlMaster auto`、`ControlPersist 8h`、`ServerAliveInterval 30`。若主连接仍超时，在 236 上查 `sshd`/防火墙；HTTP `:5198` 通而 SSH 不通时多为 **22 端口策略**，不是 Playground 问题。

3. SSH（短命令也可直接）：`cursor-agent@36.103.198.236` 或别名 **`ssh 236`**
4. 进入容器：`docker exec -it viknow2-test-dev bash`（非交互可用 `docker exec viknow2-test-dev bash -lc '...'`）
5. 在 `/workspace` 开发：
   - `git fetch origin`
   - `git checkout -B agent/<topic> origin/main`（或 rebase 到最新 `origin/main`）
   - 改代码、跑测试、`:5176` / 容器内 `:8000` 冒烟
   - `git commit` 后 `git push -u origin agent/<topic>`
6. 结束时汇报：分支名、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main...HEAD`

### ViKnow Platform Playground（`:5198`）— 改完必 push + 部署

业务仓 Gitea：`castmeta-research/viknow-platform`（请在 Gitea 上将原 `viknow3` 仓库重命名或迁移 remote）。236 源码目录：`/home/cursor-agent/src/viknow-platform`。

**只改跳板仓 `viknow-platform-work/` 或只 push GitHub 不算完成**；Playground 读的是 236 上该目录 + `scripts/deploy-playground-ui.sh` 拉起的进程。

每次改动 agent / HyperRAG / Playground 相关代码，在同一任务内做完：

1. **同步到 236**（`rsync`/`scp` 或 236 上 `git pull` 你的 `agent/<topic>` 分支）
2. **单测**：`/home/cursor-agent/viknow-platform/venv312/bin/python -m pytest`（至少本次改动涉及的用例）
3. **部署**：`cd /home/cursor-agent/src/viknow-platform && bash scripts/finish-platform-migration-236.sh`（首次改名后）或 `bash scripts/deploy-playground-ui.sh restart`
4. **冒烟**：`curl -sf http://127.0.0.1:5198/playground`；必要时在 236 上 `PYTHONPATH=src` 调 `knowledge_discover` 等
5. **推送 Gitea**：`agent/<topic>` 分支；Playground / deploy 236 相关改动 **验证通过后直接 merge 到 `main` 并 push**（无需再向负责人确认）。236 上 `git checkout main && git pull` 后部署。

`VIKNOW_236_SSH_KEY` 若为单行 PEM，需恢复换行后再 `chmod 600`，否则 `Load key: error in libcrypto`：

```bash
python3 - <<'PY'
import os, re
raw = os.environ["VIKNOW_236_SSH_KEY"]
if "BEGIN OPENSSH" in raw and "\n" not in raw.strip():
    m = re.search(r"(-----BEGIN OPENSSH PRIVATE KEY-----)(.+?)(-----END OPENSSH PRIVATE KEY-----)", raw)
    raw = f"{m.group(1)}\n{m.group(2).replace(' ', chr(10))}\n{m.group(3)}\n"
open(os.path.expanduser("~/.ssh/id_ed25519"), "w").write(raw)
PY
chmod 600 ~/.ssh/id_ed25519
```

### 阿里云操作台账（必做）

每次对 **阿里云**（ACK / ACR / RDS / Tair / OSS / RAM 等）的操作、变更、登录探测，必须追加写入 [`docs/cloud-ops-log.md`](docs/cloud-ops-log.md)（倒序、用文件内模板）。不记 AWS。禁止把密码、AK/SK、kubeconfig 原文写进该文件或任何提交。

### 禁止

- 不要在本跳板仓库里实现 ViKnow 业务功能
- 不要改、停、删 `viknow2-test` / private 相关容器
- 不要 `git push` 到 `main`
- 不要把密钥、密码、license 写进提交
- 不要假设正式 test 镜像已经等于最新 `origin/main`

### SSH 密钥落地（Secret）

Secret 名：`VIKNOW_236_SSH_KEY`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# 若 Secret 是 OpenSSH 私钥原文：
printf '%s\n' "$VIKNOW_236_SSH_KEY" > ~/.ssh/id_ed25519
# 若 Secret 是 base64：
# echo "$VIKNOW_236_SSH_KEY" | base64 -d > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 'echo ok'
```

### 冒烟命令（连通性）

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 \
  'docker exec viknow2-test-dev bash -lc "
    cd /workspace &&
    git fetch origin &&
    git checkout -B agent/cloud-smoke origin/main &&
    git commit --allow-empty -m \"chore: cloud agent smoke\" &&
    git push -u origin agent/cloud-smoke &&
    git log -1 --oneline &&
    git status -sb
  "'
```

成功标志：Gitea `castmeta-research/viknow2` 出现分支 `agent/cloud-smoke`。

### 推送 Gitea（viknow-platform / viknow2 业务仓）

`VIKNOW_236_SSH_KEY` **只能 SSH 登录 236**，不能 `git push` 到 `git.qingxiang.tech`。

任选其一：

1. **Cloud Agent 直推 Gitea**：在环境 Secrets 增加 **`VIKNOW_GITEA_SSH_KEY`**（内容为 Gitea 登记用的 **OpenSSH 私钥**全文，不是指纹）。启动后：

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$VIKNOW_GITEA_SSH_KEY" > ~/.ssh/gitea_ed25519
chmod 600 ~/.ssh/gitea_ed25519
ssh -i ~/.ssh/gitea_ed25519 -p 2222 -o StrictHostKeyChecking=accept-new -T git@git.qingxiang.tech
# 期望：Hi there, <user>! ... Gitea does not provide shell access.
export GIT_SSH_COMMAND='ssh -i ~/.ssh/gitea_ed25519 -p 2222 -o BatchMode=yes'
git -C /path/to/viknow-platform push -u origin agent/<topic>
```

校验指纹（Gitea 页上显示的 SHA256/SHA1 需与私钥一致）：

```bash
ssh-keygen -y -f ~/.ssh/gitea_ed25519 | ssh-keygen -lf -
python3 -c "import hashlib,base64,sys; b=base64.b64decode(sys.stdin.read().split()[1]); print(hashlib.sha1(b).hexdigest())" <<< "$(ssh-keygen -y -f ~/.ssh/gitea_ed25519)"
```

2. **经 236 推送**（236 上已有例如 `~/.ssh/gitea_ljs_ed25519`）：

```bash
ssh -i ~/.ssh/id_ed25519 cursor-agent@36.103.198.236 \
  'cd /home/cursor-agent/src/viknow-platform && GIT_SSH_COMMAND="ssh -i ~/.ssh/gitea_ljs_ed25519 -o BatchMode=yes" git push -u origin agent/<topic>'
```

### 207（QX-AI-07）— Tailscale，不经 236 跳板

内网 `10.168.1.207` 从公网 Cloud Agent **不可路由**。207 宿主机 **QX-AI-07** 加入 Tailscale 并 **subnet router** 宣告 `10.168.1.0/24` 后，Agent 在本 VM 上入同一 tailnet 即可操作 207。

**207 侧（一次性，root）** — 若曾改过 non-default 项，需带上 `--advertise-routes`：

```bash
sudo tailscale up --accept-routes \
  --auth-key="$TS_INSTALL_KEY" \
  --hostname=qx-ai-07 \
  --advertise-routes=10.168.1.0/24
sudo tailscale status   # 例：100.101.46.52  qx-ai-07
```

在 [Tailscale Admin Console](https://login.tailscale.com/admin/machines) **批准** `qx-ai-07` 的 subnet routes（`10.168.1.0/24`）。批准前 Cloud Agent 仍可通过 **Tailscale IP** `100.101.46.52` SSH（与 `10.168.1.207` 同机）。

**Cloud Agent 侧**

1. Tailscale install key：见 [`docs/207-tailscale-auth.md`](docs/207-tailscale-auth.md)（`scripts/tailscale-connect-207.sh` 在未设置 `VIKNOW_TAILSCALE_AUTH_KEY` 时会从该文件读取）
2. 可选：`VIKNOW207_SSH_PASSWORD` 或沿用 `VIKNOW_BASTION_SSH_PASSWORD`（SSH `wuzihan@100.101.46.52`）
3. 本 VM **无 systemd** 时需手动起 `tailscaled`，再用 `tailscale up`：

```bash
bash scripts/tailscale-connect-207.sh
bash scripts/ssh-207.sh 'hostname && ip -4 addr show tailscale0 eno1 | head -6'
```

4. 部署 ViKnow Platform + storage + PG/Redis/Neo4j：在 207 上 checkout 平台仓，配置 `~/viknow207.host.env` 后执行 `bash deploy/207/deploy-207.sh`（见 `viknow-platform-work/deploy/207/README.md`）。Actio / 模型仍指向 **236**。

**连通性说明**

| 目标 | 条件 |
|------|------|
| `100.101.46.52`（`qx-ai-07`） | Cloud Agent 已 `tailscale up --accept-routes` |
| `10.168.1.207` | 上表 + Admin 已批准 `10.168.1.0/24` subnet route |
| 不经 Tailscale 直连 `10.168.1.207` | 不可行（私网） |

**禁止**：把 **SSH 密码** 写入提交或 `docs/cloud-ops-log.md`。Tailscale install key 暂存于 `docs/207-tailscale-auth.md`（按负责人要求）；轮换后同步更新该文件。
