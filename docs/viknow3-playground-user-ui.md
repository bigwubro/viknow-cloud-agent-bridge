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
- **`/playground/workspace`**：登录后占位页（localStorage `viknow_playground_user`）
- **BFF**（直连 PG，与 `/api/v1/users` 同一 `user_service`）：
  - `POST /ui/playground/users`
  - `POST /ui/playground/login`
  - `GET /ui/playground/users/{id}`

## 236 运行

```bash
export PYTHONPATH="/home/cursor-agent/src/viknow3/src:/home/cursor-agent/src/viknow3"
export VIKNOW3_ENV_FILE=/home/cursor-agent/viknow3/runtime.env   # 含 VIKNOW_ACCESS_DSN
cd /home/cursor-agent/src/viknow3
python3 -m ui.server
```

浏览器：`http://<host>:5198/playground`（默认 `VIKNOW_UI_PORT=5198`）。

## 测试

```bash
cd /home/cursor-agent/src/viknow3
PYTHONPATH="$(pwd)/src:$(pwd)" python3 -m pytest tests/test_playground_user_ui.py -q
```

## Cloud Agent 推送状态

若 Cloud 环境 `VIKNOW_236_SSH_KEY` / Gitea 密钥无效，需在 236 容器内手动 `git push -u origin agent/ui-playground-user` 或应用 bundle。
