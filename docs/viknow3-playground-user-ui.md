# ViKnow 3.0 Playground — 用户创建 / 登录 UI

业务代码在 **Gitea `castmeta-research/viknow3`**，分支 **`agent/ui-playground-user`**（基于 `agent/hyperrag-dev3`）。

本跳板仓已附带 bundle：`docs/viknow3-ui-playground-user.bundle`（若已生成），236 上可：

```bash
cd /home/cursor-agent/src/viknow3
git fetch origin
git pull origin agent/hyperrag-dev3   # 或先 fetch bundle
# 若仅有 bundle：
# git fetch /path/to/viknow3-ui-playground-user.bundle agent/ui-playground-user:agent/ui-playground-user
git checkout agent/ui-playground-user
```

## 功能

- **OpenAI Platform 风格**：深色侧栏 + 浅色主区
- **`/playground`**：创建用户（可选显示名）、用用户 ID「登录」
- **`/playground/workspace`**：知识库列表（localStorage `viknow_playground_user`）
- **`/playground/agents`**：智能体列表（`GET /api/v1/agent/agents`，与 [Agent API 文档](http://36.103.198.236:5208/start/api/agents.html) 一致）
- **`/playground/agent/{agent_id}`**：对话页（左侧聊天 + 模型选择，右侧智能体详情；问答暂为 **Mock**，后续接 `sessions` / runs/events）
- **Access API**（与文档一致，Playground :5198 内挂载）：
  - `POST /api/v1/users`、`GET /api/v1/users/{user_id}`
  - `GET/POST /api/v1/knowledge/*`（含 `POST …/libraries/{id}/uploads`、`POST …/downloads` 预签名）
  - `GET /api/v1/agent/agents` 等
- 旧 `/ui/playground/users|login|s3/*` 已移除（404）

## 236 运行

```bash
export PYTHONPATH="/home/cursor-agent/src/viknow3/src:/home/cursor-agent/src/viknow3"
export VIKNOW3_ENV_FILE=/home/cursor-agent/viknow3/runtime.env   # 含 VIKNOW_ACCESS_DSN
cd /home/cursor-agent/src/viknow3
python3 -m ui.server
```

浏览器：`http://36.103.198.236:5198/playground`（默认 `VIKNOW_UI_PORT=5198`）。侧栏 **知识库 | 智能体** 平级切换。

## 测试

```bash
cd /home/cursor-agent/src/viknow3
PYTHONPATH="$(pwd)/src:$(pwd)" python3 -m pytest tests/test_playground_user_ui.py -q
```

## Cloud Agent 推送状态

若 Cloud 环境 `VIKNOW_236_SSH_KEY` / Gitea 密钥无效，需在 236 容器内手动 `git push -u origin agent/ui-playground-user` 或应用 bundle。
