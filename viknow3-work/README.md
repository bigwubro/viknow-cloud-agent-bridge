# viknow3

接入层使用应用层和运维层给出的 client，不在 `access/` 里实现这些 client。HyperRAG 和 Actio 也是 client：`import hyperrag` 只在 `application/hyperrag/`，`import actio` 只在 `application/actio/`。agent runtime 使用装配时注入的 `KnowledgeClient` 和 `ActioClient`。

| 接入层 | 调用 |
| --- | --- |
| `access/agent/service.py` | `application/agentcore/client.py` |
| `access/knowledge/service.py` | `application/hyperrag/client.py` |
| `access/om/service.py` | `ops/client.py` |
| `access/user/service.py` | 无。用户由调用方建立，记在接入层 |

`access/auth.py` 提供 app key 和管理凭证。业务模块使用 `Authorization: Bearer <app_key>`。OM Interface 使用管理凭证。

会话、工作区和文件留在接入层。`run` 做完这些聚合之后把这一轮交给 AgentClient。

HTTP 接口和数据说明在仓库 `docs/`，从 [docs/overview.md](docs/overview.md) 进入。

需要 Python 3.11 及以上（deepagents 的要求）。

```bash
pip install -e .
python -m viknow3
```

未设置 `VIKNOW_PORT` 时默认 `127.0.0.1:5175`；236 demo 接入层为 **5197**（见 `deploy/demo/runtime.env`）。**5175 在 236 上是 viknow2-test，不是 viknow3。** 开发用的 app key 是 `dev-app-key`，默认智能体是 `expertbuddy-deepagents`；`GET /api/v1/agent/agents` 列出全部已注册智能体（不按 app key 过滤）。管理凭证是 `dev-admin-key`。

存储只使用同一台 Postgres，按业务分库。`VIKNOW_ACCESS_DSN` 是库 `viknow3_access`、schema `access`：`users`、`sessions`、`workspaces`、`files`、`run_refs`（一轮的 `input`、`output`、`model`、`status`、`citations`、`token_usage`）、`run_events`（主键 `(run_id, sequence)`）、`run_files`（一轮用过哪些文件）。进行中的 SSE 和结束后的 `GET .../history` 读的都是 `run_events`，进程重启后照样能回放。终态事件和 run 的结束字段在同一个事务里写入。删除只写 `deleted_at`：删 session 在同一个事务里给 session、workspace、file、run、event、run_file 写入同一个时间，删 user 再加上 user 本身；其下有 `running` 的 run 时返回 409 `session_busy`。进程启动时，库里仍是 `running`、但本进程没有对应 task 的 run 记为 `failed`，并补一条 `run_failed`（`code` 为 `run_lost`）。`VIKNOW_AGENT_DSN` 是库 `viknow3_agent`。DeepAgents 的 checkpoint 在 schema `checkpoint`（`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations`）。ADK 的 session 在 schema `adk`。thread id 由 runtime 用 user、workspace、session 拼出。外壳只把这三项标识传进去，不解析 checkpoint。`VIKNOW_HYPERRAG_DSN` 是库 `viknow3_knowledge`：目录在 schema `catalog`，HyperRAG 表在 `hyperrag`，向量在 `hyperrag_kg`。运维配置在库 `viknow3_ops`、schema `ops`。

Agent Service 在 `application/agentcore`。进程里有一个常驻的 asyncio agent loop（`runtime/loop.py`，单独的守护线程），每一轮是这个 loop 上的一个 task。`runtime/host.py` 按 profile 的 `runtime` 把这一轮交给 `deepagents_adapter` 或 `adk_adapter`。DeepAgents 适配器调用 `src/agents/runtimes/deepagents`，ADK 适配器调用 `src/agents/runtimes/adk`。两边都把模型 token、工具调用和 token 用量写成 `run_events`。`interrupt` 取消这个 task。同一 user、workspace、session 的前几轮由 runtime 写进 `VIKNOW_AGENT_DSN` 的 LangGraph checkpoint。上一轮被中断、checkpoint 里还留着没有结果的 tool call 时，下一轮会先补上 `tool call did not finish (run interrupted)`，再送出新的用户消息。每个 task 有自己的 contextvars，本轮可查的库只对本轮工具可见。已登记的 agent 由 `src/agents/layout.py` 扫描 `src/agents/profiles/*.yaml` 得到。对外 id 是 `expertbuddy-deepagents`（`profiles/expert_buddy.yaml`，回合 `runtimes/deepagents`）和 `expertbuddy-adk`（`profiles/expert_buddy_adk.yaml`，回合 `runtimes/adk`）。技能正文在 `src/agents/skills/`。`VIKNOW_USE_SCRIPT_MODEL=1` 时换成脚本模型，只给测试用。

配置在库 `viknow3_ops` 的 schema `ops`，不和业务表放在一起。地址和字段加密密钥来自 `VIKNOW_OPS_DSN`、`VIKNOW_OPS_SECRET`。这两个变量没有设置时，进程会读 `~/.viknow3/ops.env`。agent 的主模型取 `agent_model.model_id`。管理接口是 `GET/POST /api/v1/om/config`，变更记录在 `/api/v1/om/config/changes`。测试库是同一个实例上的 `viknow3_ops_test`。

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
