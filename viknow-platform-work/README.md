# viknow-platform

236 / Gitea 从旧仓名迁移：在 Gitea 将仓库重命名为 **`castmeta-research/viknow-platform`**，236 上 `git remote set-url origin ssh://git@git.qingxiang.tech:2222/castmeta-research/viknow-platform.git`，源码目录 **`/home/cursor-agent/src/viknow-platform`**（venv/state 建议同步迁到 `/home/cursor-agent/viknow-platform/`）。部署 env 旧键 **`VIKNOW3_*`** 已改为 **`VIKNOW_PLATFORM_ROOT` / `VIKNOW_DATA_DIR` / `VIKNOW_LIBRARIES_ROOT` / `VIKNOW_APP_KEY`** 等（见 `deploy/236/env/ENV.md`）；236 上跑一遍 `refresh-secrets-from-host.sh` 并改 `secrets.env`。知识库 Postgres 库名示例改为 **`viknow_knowledge`**（生产库若仍为旧名，DSN 可暂不改库名）。

项目边界（什么属于 / 不属于本仓）：[`docs/project-boundary.md`](docs/project-boundary.md)。

接入层使用应用层和运维层给出的 client，不在 `access/` 里实现这些 client。

| 接入层 | 调用 |
| --- | --- |
| `access/agent/service.py` | `application/agentcore/client.py` |
| `access/rag/service.py` | `application/hyperrag/client.py` |
| `access/learning/service.py` | `application/learning/client.py` |
| `access/om/service.py` | `ops/client.py` |
| `access/user/service.py` | 无。用户由调用方建立，记在接入层 |

`access/auth.py` 提供 app key 和管理凭证。前四个模块使用 `Authorization: Bearer <app_key>`。OM Interface 使用管理凭证。

会话、工作区和文件留在接入层。`run` 做完这些聚合之后把这一轮交给 AgentClient。

HTTP 接口以文档站为准：对话（会话、轮次、事件、删除）见 <http://36.103.198.236:5208/start/api/runs.html>，表结构见 <http://36.103.198.236:5208/model/session.html>。

需要 Python 3.11 及以上（deepagents 的要求）。

```bash
pip install -e .
python -m viknow_platform
```

未设置 `VIKNOW_PORT` 时默认 `127.0.0.1:5175`；236 Playground API 为 **5218**（见 `deploy/236/env/api.env`）。**5175 在 236 上是 viknow2-test，不是 ViKnow Platform。** 开发用的 app key 是 `dev-app-key`，默认智能体是 `deepagents`；`GET /api/v1/agent/agents` 列出全部已注册智能体（不按 app key 过滤）。管理凭证是 `dev-admin-key`。

存储只使用同一台 Postgres，按业务分库。`VIKNOW_ACCESS_DSN` 是会话库：`users`、`sessions`、`workspaces`、`files`、`run_refs`（一轮的 `input`、`output`、`model`、`status`、`citations`、`token_usage`）、`run_events`（主键 `(run_id, sequence)`）、`run_files`（一轮用过哪些文件）。进行中的 SSE 和结束后的 `GET .../history` 读的都是 `run_events`，进程重启后照样能回放。终态事件和 run 的结束字段在同一个事务里写入。删除只写 `deleted_at`：删 session 在同一个事务里给 session、workspace、file、run、event、run_file 写入同一个时间，删 user 再加上 user 本身；其下有 `running` 的 run 时返回 409 `session_busy`。进程启动时，库里仍是 `running`、但本进程没有对应 task 的 run 记为 `failed`，并补一条 `run_failed`（`code` 为 `run_lost`）。`VIKNOW_AGENT_DSN` 是 agent 库，只放 LangGraph checkpointer 的表（`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations`），`thread_id` 就是 `session_id`。`VIKNOW_HYPERRAG_DSN` 是知识库目录 `knowledge_libraries`、`knowledge_assets`。运维配置仍在 `viknow_ops`。

Agent Service 在 `application/agentcore`，不依赖 Agno。进程里有一个常驻的 asyncio agent loop（`runtime/loop.py`，单独的守护线程），每一轮是这个 loop 上的一个 task：`runtime/host.py` 按 profile 建图，`runtime/langgraph_runner.py` 用 `astream_events(version="v2")` 把模型 token、工具调用、token 用量映射成 ViKnow 事件。`interrupt` 取消这个 task。同一 `session_id` 的前几轮由 Postgres checkpointer 保存，上一轮被中断时没答完的 tool call 会在下一轮开头补上结果。每个 task 有自己的 contextvars，本轮可查的库只对本轮工具可见。注册的智能体只有两个：`deepagents`（`create_deep_agent`，viknow-v2 profile）和 `adk`（进程内 Google ADK `LlmAgent`，同一套 viknow-v2 工具）。`adk` 始终在列表中可见；未安装 `google-adk` 时 run 失败并返回 `adk_not_configured`。`VIKNOW_USE_SCRIPT_MODEL=1` 时换成脚本模型，只给测试用。

当前仓库里的其余 client 仍是占位实现，用来把接入层和 SDK 的契约跑通。

配置在单独的 Postgres 实例里，schema 是 `viknow_ops`，不和业务库放在一起。地址和字段加密密钥来自 `VIKNOW_OPS_DSN`、`VIKNOW_OPS_SECRET`。这两个变量没有设置时，进程会读 `~/.viknow-platform/ops.env`。`deepagents` 与 `adk` 的主模型取 `agent_model.model_id`。管理接口是 `GET/POST /api/v1/om/config`，变更记录在 `/api/v1/om/config/changes`。测试库是同一个实例上的 `viknow_ops_test`。

## 文档站 CI（Docusaurus）

- 正文目录：仓库根目录 `docs/`（Markdown）
- 站点工程：`website/`（Docusaurus 3.9）
- Workflow：`.gitea/workflows/docs-site.yml`（Gitea Actions，runner `python-312-uv`）
- 发布目标：236 宿主机 `/data/wuzihan/docusaurus`，对外 http://36.103.198.236:3022/

`main` 上变更 `docs/**` 或 `website/**` 会自动构建并发布；也可在 Gitea **Actions → Docs site → Run workflow** 手动触发。

本地构建：

```bash
bash scripts/deploy-docs-site.sh
```

（需能写入发布目录；CI 在 act_runner 上默认使用 `/cache/npm`。）
