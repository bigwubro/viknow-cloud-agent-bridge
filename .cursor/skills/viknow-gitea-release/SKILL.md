---
name: viknow-gitea-release
description: >-
  Open, merge, and deploy viknow2 to :5175 via Gitea Actions.
  Use when the user asks to 发版, 合 main, 开 PR, merge to main, run
  ci.yml, dispatch deploy-test, ship to 5175, or operate
  castmeta-research/viknow2 release/CI/deploy. Do not use for 5176
  overlay-only experiments.
---

# viknow2 Gitea 发版

操作 Gitea `castmeta-research/viknow2` 时按此流程。试验 overlay（5176）**不走**本 skill；只有明确要上 `:5175` 才合仓发版。

## 凭证

在共用机 `source` Gitea env，再用 `$GITEA_URL` / `$GITEA_TOKEN`（或该 env 里实际导出的变量）调 API。

- 不要把 token、密码写进聊天、PR、commit、日志。
- 不要 `echo` / `cat` 含 token 的 env 文件。
- curl 用 `Authorization: token $GITEA_TOKEN`，不要把 token 打进命令历史可见处以外的输出。

默认 API 根（若 env 未提供）：`https://git.qingxiang.tech/api/v1`  
仓库：`castmeta-research/viknow2`

```bash
# 示例：先 source，再检查变量是否存在（不要打印值）
: "${GITEA_TOKEN:?GITEA_TOKEN missing}"
GITEA_API="${GITEA_URL:-https://git.qingxiang.tech/api/v1}"
REPO="castmeta-research/viknow2"
```

## 硬约定

- 5176 overlay / `viknow2-test-dev` 上的试验 **不走 Gitea 发版**。
- 合 `main` 前确认 CI 相关改动已在本地或 5176 验过。
- **一次只 dispatch 一发** `deploy-test.yml`。两发并发会抢 `viknow2-test` 容器 rename。
- 不要 `git push` 到 `main`；只通过 Gitea PR merge。
- 不要改、停、删 `viknow2-test` / private 相关容器（残留 rollback 交给运维清）。
- 不要假设正式 test 镜像已经等于最新 `origin/main`，直到本次 deploy-test 绿。

## 1. 开 PR / 合 main

1. 确认分支 tip SHA，以及它基于的 `main` SHA（`git fetch` 后看 `origin/main`）。
2. 开 PR（head → `main`）：

```bash
curl -sS -X POST \
  -H "Authorization: token $GITEA_TOKEN" \
  -H "Content-Type: application/json" \
  "$GITEA_API/repos/$REPO/pulls" \
  -d "{\"title\":\"...\",\"head\":\"<branch>\",\"base\":\"main\",\"body\":\"...\"}"
```

3. 合 PR（`Do=merge`），记下 **merge commit SHA**：

```bash
curl -sS -X POST \
  -H "Authorization: token $GITEA_TOKEN" \
  -H "Content-Type: application/json" \
  "$GITEA_API/repos/$REPO/pulls/<n>/merge" \
  -d '{"Do":"merge"}'
```

4. 合完后 Actions 通常会自动跑 `ci.yml`。没有就手动 dispatch，`ref=main`：

```bash
curl -sS -X POST \
  -H "Authorization: token $GITEA_TOKEN" \
  -H "Content-Type: application/json" \
  "$GITEA_API/repos/$REPO/actions/workflows/ci.yml/dispatches" \
  -d '{"ref":"main"}'
```

## 2. 等 CI 绿

1. 查任务 / runs，盯 **merge SHA** 上的 `ci.yml` Build：

```bash
curl -sS -H "Authorization: token $GITEA_TOKEN" \
  "$GITEA_API/repos/$REPO/actions/tasks"
# 或
curl -sS -H "Authorization: token $GITEA_TOKEN" \
  "$GITEA_API/repos/$REPO/actions/runs/<id>/jobs"
```

2. 确认步骤全绿：**Lint → Tests → Build wheel → Store artifact**。
3. 红了：把失败步骤与关键报错发群，等代码修。**不要**强行 `deploy-test`。

## 3. 发 :5175（deploy-test）

仅 CI 绿后：

```bash
curl -sS -X POST \
  -H "Authorization: token $GITEA_TOKEN" \
  -H "Content-Type: application/json" \
  "$GITEA_API/repos/$REPO/actions/workflows/deploy-test.yml/dispatches" \
  -d '{"ref":"main"}'
```

盯**同一次 run** 的 **Deploy to test** 与 **Check test** 都绿。

| 现象 | 处理 |
|---|---|
| Check：`container probe timed out`，但宿主机 `/api/v1/health/license`、`/login`、`/chat` 已 200 | 启动竞态。运维确认后仍喊测试复验，或视情况再 **一发** rerun |
| Deploy：卡在 `rename ... viknow2-test-deploy-rollback` 同名 400 | 先让运维清残留 rollback 容器名，再重跑 **一发** |
| 失败 run 需重跑 | `POST .../actions/runs/{id}/rerun`；仍避免与另一发并发 |

## 4. 发版后

在群里报：

- merge SHA
- CI run
- deploy-test run
- 前端：`http://36.103.198.236:5175`

然后 `@viknow测试哥` 复验；需要时 `@飞书任务管理者` 补看板。

## API 速查

| 动作 | 路径 |
|---|---|
| 开/列 PR | `/repos/castmeta-research/viknow2/pulls` |
| 合 PR | `/repos/castmeta-research/viknow2/pulls/{n}/merge` |
| 触发工作流 | `/actions/workflows/{ci.yml\|deploy-test.yml}/dispatches` |
| 重跑失败 run | `POST /actions/runs/{id}/rerun` |
| 查任务 | `/actions/tasks` 或 `/actions/runs/{id}/jobs` + logs |

## 不要做

- 并发两发 `deploy-test`
- 把 `GITEA_TOKEN` 贴进群或聊天
- 把 5176 overlay 误当成已合仓发版
- CI 红仍然 dispatch `deploy-test`
- 在跳板仓库实现 ViKnow 业务功能来「代替」这次发版
