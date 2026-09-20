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

### Cursor My Secrets（Cloud Agent 注入）

在 **Cloud Agents → My Secrets** 配置（Runtime Secret，All Repositories）。**仅在「新开 Agent / 新 Pod」时注入**；同一条长会话里 Save Secret **不会**热更新当前进程。

| Secret | 用途 |
| --- | --- |
| `VIKNOW_236_SSH_KEY` | SSH 236 |
| `VIKNOW_BASTION_SSH_PASSWORD` | 可选跳板 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | 236 上 `aliyun` / `fetch-ack-kubeconfig.sh` |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 同上 |

自检（新 Agent 第一条命令应看到 4 个名字）：

```bash
echo "$CLOUD_AGENT_INJECTED_SECRET_NAMES"
```

若仍只有 2 个：Archive 当前 Agent → **New Agent**；仍不行则在 [Environment 面板](https://cursor.com/dashboard/cloud-agents/environments/e/f0d7279a-8b39-11f1-b532-320a589b8025) **Rebuild / Save** 后再 New Agent。

Gitea 发版用 **`VIKNOW_ACK_KUBECONFIG`** 等（在 Gitea `viknow2` 仓库 Secrets，与 Cursor My Secrets 不是同一处）。

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
