# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
- API / 数据模型文档站（实现以它为准）：<http://36.103.198.236:5208/>，对话接口 <http://36.103.198.236:5208/start/api/runs.html>，会话数据模型 <http://36.103.198.236:5208/model/session.html>
- Cloud Agent 在本仓库启动后，必须 SSH 到 236 再 `docker exec` 进入开发容器

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600）
2. **236 用 SSH 长连接（ControlMaster）**，不要每条命令新建 TCP（易被 reset / `Exceeded MaxStartups`）：

```bash
# ~/.ssh/config 片段（Cloud Agent 镜像里应包含 Host 36.103.198.236 与 Host viknow236 两段，Control 参数一致）
Host viknow236 36.103.198.236
  HostName 36.103.198.236
  User cursor-agent
  IdentityFile ~/.ssh/id_ed25519
  StrictHostKeyChecking accept-new
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 45m

ssh viknow236 '...'   # 必须用别名或 IP 且命中上述 Host，禁止 ssh -i ... cursor-agent@36.103.198.236 绕过 config
# 同一任务里所有 236 操作共用一个 master；多条命令用一次 ssh 包 bash -s，不要 scp+ssh 连打
```

**为何以前经常 SSH 失败**

| 现象 | 原因 |
|------|------|
| `Exceeded MaxStartups`（`ssh -v` 里 banner 一行） | 短时间内太多**未认证**的新 TCP 连接；sshd 默认 `MaxStartups` 会直接关掉新连接 |
| `Connection reset by peer` / `Connection closed`（握手阶段） | 同上，或刚被限流后仍在**秒级重试** |
| 偶发能连、连打又挂 | 未走 ControlMaster：`ssh -i ... user@IP` **不匹配** `Host viknow236`，每条命令都是新连接 |

**Agent 必须遵守**

- 只连 `ssh viknow236 '…'` 或 `ssh 36.103.198.236 '…'`（且 `~/.ssh/config` 里为该 IP 配置了 ControlMaster）。
- **禁止** `for i in 1 2 3 4 5; do ssh … || sleep 2; done` 这类风暴重试；失败一次后**等待 2～3 分钟**再试**一次**，或改用公网 HTTP（如 Playground API `:5218`）。
- **禁止**同一轮里交替 `scp` + `ssh` 十几次；传补丁用**单次** `ssh viknow236 'patch …'` 或 rsync over multiplex。
- viknow3 冒烟、拉 agent trace 优先 `http://36.103.198.236:5218`，不必为每条 API 再开 SSH。

3. 进入容器：`docker exec -it viknow2-test-dev bash`（非交互可用 `docker exec viknow2-test-dev bash -lc '...'`）
4. 在 `/workspace` 开发（viknow2 容器内；viknow3 Playground 见下节 `agent/<topic>` 或 `main` 约定）：
   - `git fetch origin && git checkout main && git pull`
   - 改代码、跑测试、`:5176` / 容器内 `:8000` 冒烟
   - `git commit` 后 `git push origin main`
5. 结束时汇报：`main` 上的 `git log -1 --oneline`、`git diff --stat HEAD~1`

### viknow3 Playground（`:5198`）— 改完必 push + 部署

业务仓 Gitea：`castmeta-research/viknow3`。236 源码目录：`/home/cursor-agent/src/viknow3`。

**只改跳板仓 `viknow3-work/` 或只 push GitHub 不算完成**；Playground 读的是 236 上该目录 + `scripts/deploy-playground-ui.sh` 拉起的进程。

每次改动 agent / HyperRAG / Playground 相关代码，在同一任务内做完：

1. **同步到 236**（`rsync`/`scp`，或 236 上 `git pull` 你刚推进的 `main`）
2. **单测**：`/home/cursor-agent/viknow3/venv312/bin/python -m pytest`（至少本次改动涉及的用例）
3. **部署**：`cd /home/cursor-agent/src/viknow3 && bash scripts/deploy-playground-ui.sh restart`
4. **冒烟**：`curl -sf http://127.0.0.1:5198/playground`；必要时在 236 上 `PYTHONPATH=src` 调 `knowledge_discover` 等
5. **推送 Gitea `main`**，并同步 GitHub 跳板仓 `main`。同一次任务里做完，不要停在功能分支上等用户说合入。

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
- 做完的改动在同一次任务里推进 `main`（Gitea 业务仓和 GitHub 跳板仓）。直接 push 被拒绝时，自己开 PR 并合并，不要等用户说「合」
- 不要把密钥、密码、license 写进提交
- 不要假设正式 test 镜像已经等于最新 `origin/main`
- 不要对 236 **短时间多次** `ssh`/`scp` 重试；不要绕过 `~/.ssh/config` 的 ControlMaster 直连 `cursor-agent@36.103.198.236`

### SSH 密钥落地（Secret）

Secret 名：`VIKNOW_236_SSH_KEY`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# 若 Secret 是 OpenSSH 私钥原文：
printf '%s\n' "$VIKNOW_236_SSH_KEY" > ~/.ssh/id_ed25519
# 若 Secret 是 base64：
# echo "$VIKNOW_236_SSH_KEY" | base64 -d > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh viknow236 'echo ok'
```

### 冒烟命令（连通性）

```bash
ssh viknow236 \
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

### 推送 Gitea（viknow3 / viknow2 业务仓）

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
git -C /path/to/viknow3 push origin main
```

校验指纹（Gitea 页上显示的 SHA256/SHA1 需与私钥一致）：

```bash
ssh-keygen -y -f ~/.ssh/gitea_ed25519 | ssh-keygen -lf -
python3 -c "import hashlib,base64,sys; b=base64.b64decode(sys.stdin.read().split()[1]); print(hashlib.sha1(b).hexdigest())" <<< "$(ssh-keygen -y -f ~/.ssh/gitea_ed25519)"
```

2. **经 236 推送**（236 上已有例如 `~/.ssh/gitea_ljs_ed25519`）：

```bash
ssh viknow236 \
  'cd /home/cursor-agent/src/viknow3 && GIT_SSH_COMMAND="ssh -i ~/.ssh/gitea_ljs_ed25519 -o BatchMode=yes" git push origin main'
```
