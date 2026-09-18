# ViKnow Cloud Agent Bridge

本仓库是 **Cursor Cloud Agent 跳板**，不是 ViKnow 业务代码仓。

- 业务仓在 Gitea：`ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow2.git`
- 真实工作区在 236：`viknow2-test-dev` 容器内的 `/workspace`
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

### 代码变更与部署（业务仓 viknow2）

- 改完代码后 **只** `git commit` 并 `git push origin main`（或先推 `agent/<topic>` 再合并进 `main`）
- **禁止热更新**：不对 `viknow2-test`、`viknow2-test-dev` 等运行中容器打补丁（禁止 `docker cp`、直接改容器内文件、改 `site-packages`、为让补丁生效而 `docker restart`）
- 正式环境（如 `viknow2-test` `:5175`）以镜像为准；`main` 上的改动需等 **重建镜像 / 发版** 后才生效
- 开发验证在 `viknow2-test-dev`（`:5176`）内拉最新 `main` 跑测试即可，不要把补丁打进 `viknow2-test`

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
