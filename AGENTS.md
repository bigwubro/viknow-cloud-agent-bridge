# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
- API / 数据模型文档站（实现以它为准）：<http://36.103.198.236:5208/>，对话接口 <http://36.103.198.236:5208/start/api/runs.html>，会话数据模型 <http://36.103.198.236:5208/model/session.html>
- Cloud Agent 在本仓库启动后，必须 SSH 到 236 再 `docker exec` 进入开发容器

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600）
2. SSH：`cursor-agent@36.103.198.236`
3. 进入容器：`docker exec -it viknow2-test-dev bash`（非交互可用 `docker exec viknow2-test-dev bash -lc '...'`）
4. 在 `/workspace` 开发：
   - `git fetch origin`
   - `git checkout -B agent/<topic> origin/main`（或 rebase 到最新 `origin/main`）
   - 改代码、跑测试、`:5176` / 容器内 `:8000` 冒烟
   - `git commit` 后 `git push -u origin agent/<topic>`
5. 结束时汇报：分支名、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main...HEAD`

### viknow3 Playground（`:5198`）— 改完必 push + 部署

业务仓 Gitea：`castmeta-research/viknow3`。236 源码目录：`/home/cursor-agent/src/viknow3`。

**只改跳板仓 `viknow3-work/` 或只 push GitHub 不算完成**；Playground 读的是 236 上该目录 + `scripts/deploy-playground-ui.sh` 拉起的进程。

每次改动 agent / HyperRAG / Playground 相关代码，在同一任务内做完：

1. **同步到 236**（`rsync`/`scp` 或 236 上 `git pull` 你的 `agent/<topic>` 分支）
2. **单测**：`/home/cursor-agent/viknow3/venv312/bin/python -m pytest`（至少本次改动涉及的用例）
3. **部署**：`cd /home/cursor-agent/src/viknow3 && bash scripts/deploy-playground-ui.sh restart`
4. **冒烟**：`curl -sf http://127.0.0.1:5198/playground`；必要时在 236 上 `PYTHONPATH=src` 调 `knowledge_discover` 等
5. **推送 Gitea**：`agent/<topic>` 分支（见下节「推送 Gitea」）

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
git -C /path/to/viknow3 push -u origin agent/<topic>
```

校验指纹（Gitea 页上显示的 SHA256/SHA1 需与私钥一致）：

```bash
ssh-keygen -y -f ~/.ssh/gitea_ed25519 | ssh-keygen -lf -
python3 -c "import hashlib,base64,sys; b=base64.b64decode(sys.stdin.read().split()[1]); print(hashlib.sha1(b).hexdigest())" <<< "$(ssh-keygen -y -f ~/.ssh/gitea_ed25519)"
```

2. **经 236 推送**（236 上已有例如 `~/.ssh/gitea_ljs_ed25519`）：

```bash
ssh -i ~/.ssh/id_ed25519 cursor-agent@36.103.198.236 \
  'cd /home/cursor-agent/src/viknow3 && GIT_SSH_COMMAND="ssh -i ~/.ssh/gitea_ljs_ed25519 -o BatchMode=yes" git push -u origin agent/<topic>'
```
