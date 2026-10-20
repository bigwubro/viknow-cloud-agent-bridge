# Playground 验收清单（236）

**环境**：http://36.103.198.236:5198/playground  
**API 文档**：[Users](http://36.103.198.236:5208/start/api/users.html) · [Knowledge](http://36.103.198.236:5208/start/api/knowledge.html) · [Agents](http://36.103.198.236:5208/start/api/agents.html)

## 原则

- 浏览器业务数据 **只调** `/api/v1/*`（Playground 进程内挂载 access 路由，服务端注入 app key）。
- **不再使用** `/ui/playground/users|login|s3/*`（应 404）。
- 智能体对话 **Mock**（前端 `agent.js`），真实 runs/events 后续再接。

## 自动化冒烟

236 上：

```bash
cd /home/cursor-agent/src/viknow3
chmod +x scripts/playground-acceptance.sh
./scripts/playground-acceptance.sh
# 或 PLAYGROUND_BASE=http://36.103.198.236:5198 ./scripts/playground-acceptance.sh
```

## 手工验收（建议明天按此走一遍）

| # | 步骤 | 期望 API / 行为 |
|---|------|-----------------|
| 1 | 打开 `/playground`，创建用户 | `POST /api/v1/users` |
| 2 | 用用户 ID 进入工作区 | `GET /api/v1/users/{id}` |
| 3 | 知识库：新建库、进入库 | `POST/GET /api/v1/knowledge/libraries` |
| 4 | 上传文件 | `POST …/libraries/{id}/uploads` → S3 PUT → `POST …/assets` |
| 5 | 预览/下载 | `POST /api/v1/knowledge/downloads` |
| 6 | 智能体列表 | `GET /api/v1/agent/agents` |
| 7 | 进入对话、选模型、提问 | `GET …/agents/{id}`、`GET …/models`；回复为 Mock |

## 单测

```bash
export VIKNOW3_ENV_FILE=/home/cursor-agent/viknow3/pg-demo.env:/home/cursor-agent/.viknow3/ops.env
export PYTHONPATH="$(pwd)/src:$(pwd)"
python3 -m pytest tests/test_playground_user_ui.py -q
```
