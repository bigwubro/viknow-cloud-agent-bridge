# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- **Canonical 工作区（改代码、`git`）**：236 宿主机 `/home/cursor-agent/work/viknow2`
- Cloud Agent 在本仓库启动后，**先 SSH 到 236**，在宿主机工作区改代码、提交、推送；`docker exec viknow2-test-dev` **仅**用于在容器内对已 `git pull` 的同一仓库跑 pytest / `:5176` 冒烟（见下文「禁止热更新」）

## Cursor Cloud specific instructions

### 必做

1. 用 Secret `VIKNOW_236_SSH_KEY` 写入 `~/.ssh/id_ed25519`（chmod 600；若 Secret 是单行 OpenSSH 私钥，需还原为带换行的 PEM 格式，见下文）
2. SSH：`cursor-agent@36.103.198.236`
3. （可选，仅跑测/冒烟）进入开发容器：`docker exec -it viknow2-test-dev bash`（非交互可用 `docker exec viknow2-test-dev bash -lc '...'`）
4. 在 **宿主机**工作区开发：
   - `cd /home/cursor-agent/work/viknow2`
   - `git fetch origin`
   - `git checkout -B agent/<topic> origin/main`（或 rebase 到最新 `origin/main`）
   - 改代码、跑测试（优先宿主机 `uv run pytest` / `PYTHONPATH=src python -m pytest`；若必须在容器内跑，先在容器内对**同一 git 目录** `git pull`，再执行 pytest）
   - `:5176` / 容器内 `:8000` 冒烟仅作辅助，**不能**替代「推上 Gitea」
   - `git commit` 后 `git push`（按任务要求推 `agent/<topic>` 或合并进 `origin/main`）
5. 结束时汇报：分支名、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main...HEAD`（若已合并进 `main`，汇报 `main` 上相关提交）

### 代码变更与部署（业务仓 viknow2）

- **交付物**：Gitea `castmeta-research/viknow2` 上已合并的提交（通常为 `main`）。Cloud Agent 的任务以 **合码进 `main`（或约定分支）并 push 成功** 为完成；**不要求**、**不允许**为「验证改动」去改 `viknow2-test`（`:5175`）或向任何运行中容器热更。
- 改完代码后 **只** `git commit` 并 `git push origin main`（或先推 `agent/<topic>` 再合并进 `main`）
- **禁止热更新**：不对 `viknow2-test`、`viknow2-test-dev` 等运行中容器打补丁（禁止 `docker cp`、直接改容器内文件、改 `site-packages`、为让补丁生效而 `docker restart`）
- 正式环境（如 `viknow2-test` `:5175`）以镜像为准；`main` 上的改动需等 **重建镜像 / 发版** 后才生效
- 开发验证：在宿主机工作区跑测试；若用 `viknow2-test-dev`（`:5176`），仅在容器内 **`git fetch` / `git pull` 到与宿主机相同的提交** 后跑测试，**不要**把补丁打进 `viknow2-test` 或 dev 镜像目录

### 禁止

- 不要在本跳板仓库里实现 ViKnow 业务功能
- 不要改、停、删 `viknow2-test` / private 相关容器
- 不要在本跳板仓库 `git push` 到 `main`（业务仓 `viknow2` 在 236 上按约定推 `main`）
- 不要把密钥、密码、license 写进提交
- 不要假设正式 test 镜像已经等于最新 `origin/main`

### SSH 密钥落地（Secret）

Secret 名：`VIKNOW_236_SSH_KEY`

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# 若 Secret 是 OpenSSH 私钥原文（多行）：
printf '%s\n' "$VIKNOW_236_SSH_KEY" > ~/.ssh/id_ed25519
# 若 Secret 是单行（BEGIN/END 在同一行），用 Python 等工具在 BEGIN/END 与 base64 体之间插入换行后再写入
# 若 Secret 是 base64：
# echo "$VIKNOW_236_SSH_KEY" | base64 -d > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh-keygen -y -f ~/.ssh/id_ed25519 >/dev/null  # 校验可读
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 'echo ok'
```

Gitea push 在 236 上通常使用 `cursor-agent` 账号下已配置的 deploy key（如 `~/.ssh/gitea_*`），与登录 236 的 `VIKNOW_236_SSH_KEY` 可能不是同一把钥。

### 冒烟命令（连通性）

在 **宿主机工作区**验证 SSH + git + Gitea（不依赖容器内 `/workspace`）：

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new cursor-agent@36.103.198.236 \
  'cd /home/cursor-agent/work/viknow2 &&
    git fetch origin &&
    git checkout -B agent/cloud-smoke origin/main &&
    git commit --allow-empty -m "chore: cloud agent smoke" &&
    GIT_SSH_COMMAND="ssh -i ~/.ssh/gitea_ljs_ed25519 -o StrictHostKeyChecking=accept-new" git push -u origin agent/cloud-smoke &&
    git log -1 --oneline &&
    git status -sb'
```

成功标志：Gitea `castmeta-research/viknow2` 出现分支 `agent/cloud-smoke`。
