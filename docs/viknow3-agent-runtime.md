# viknow3 Agent 运行时（方案 B：去掉 Agno）

业务仓：Gitea `castmeta-research/viknow3`，分支 `agent/viknow-v3-knowledge-mvp`（236：`/home/cursor-agent/src/viknow3`，Playground `:5198`）。

接口以文档站为准：对话 <http://36.103.198.236:5208/start/api/runs.html>，会话数据模型 <http://36.103.198.236:5208/model/session.html>。

## 为什么换

旧实现把 LangGraph 图包进 Agno `LangGraphAgent`，每个 run 在 Agno 的工作线程里跑。`ChatOpenAI` 内部的 `httpx.AsyncClient` 绑定在创建它的事件循环上，下一个 run 换了线程/循环后复用连接，就出现 `RuntimeError: Event loop is closed`，表现为界面停在 `user_message` 之后。Agno 同时还在 `viknow_agent.ai.agno_*` 另存一份会话，和接入层的 `run_refs` 重复。

## 现在的结构

| 部件 | 文件（`src/viknow3/application/agentcore/runtime/`） | 作用 |
|------|------|------|
| Agent loop | `loop.py` | 进程内一个常驻 asyncio 循环（守护线程）。每个 run 是这个循环上的一个 task。HTTP 客户端、checkpointer 连接池都只绑定这一个循环 |
| Host | `host.py` | 按 profile 建图（每个 run 重新建，模型配置改了立即生效），绑定本 run 的库授权（contextvars，task 之间互不可见） |
| 事件映射 | `langgraph_runner.py`、`events.py` | `astream_events(version="v2")` → `model_call_started` / `message_delta` / `tool_call_started` / `tool_call_completed`（带 `tool_call_id`）/ `message_completed`（真实 token 用量）/ `run_completed`。终态只写一次，状态先于终态事件落定 |
| 会话记忆 | `checkpoint.py` | LangGraph `AsyncPostgresSaver`，库 `VIKNOW_AGENT_DSN`，`thread_id = session_id`。上一轮被中断时没答完的 tool call，下一轮开头补一条结果，避免模型接口拒收 |
| 中断 | `environment/orchestrator.py` | `task.cancel()`；2 秒内 task 没退出就由编排器关闭 run，并在日志里打印 task 当前栈 |
| A2A | `a2a.py` | `adk-qa`：JSON-RPC `message/send`，`contextId = session_id`（`VIKNOW_ADK_BASE_URL` 设置时登记） |
| 事件存储 | `events/sink.py` → `viknow3/store.py` | 事件写 `viknow_access.run_events`（主键 `(run_id, sequence)`），SSE 和 `GET .../history` 读同一张表，重启后照样回放。终态事件与 run 的 `status/output/citations/token_usage` 同一事务写入。进程启动时把库里仍是 `running`、本进程没有 task 的 run 记为 `failed`（`run_failed`，`code=run_lost`） |

Agent：`viknow-v3`（`create_deep_agent`，提示词、工具、skills 取自 viknow-v2 profile；deepagents 是必需依赖）、`manual-qa`（`knowledge_search` + 真实模型）、`langgraph-qa` / `other-agent`（回显图）、`deepagents-demo`。`VIKNOW_USE_SCRIPT_MODEL=1` 只在测试里换脚本模型。

模型：`/api/v1/agent/models` 第一项是 `server-default`（标注实际模型 `vllm/Qwen/Qwen3.6-35B-A3B-FP8`，即 236 私网 Qwen3.6），Playground 默认选它；此前默认落到 SiliconFlow。

## 删除（对照 runs.html）

| 接口 | 行为 |
|------|------|
| `DELETE /api/v1/agent/agents/{agent_id}/sessions/{session_id}?user_id=` | `user_id` 必填（缺失 422），与会话不符或不存在 404；有 `running` 的 run 时 409 `session_busy`；否则同一事务给 session、workspace、file、run、event、run_file 写同一个 `deleted_at`，204 无响应体 |
| `DELETE /api/v1/users/{user_id}/sessions/{session_id}` | 同上（此前路由在但 store 缺方法，会 500） |
| `DELETE .../sessions/{session_id}/runs/{run_id}` | 软删该轮及其 event、run_file；`running` 时 409 `run_busy` |
| `DELETE /api/v1/users/{user_id}` | 同一事务删 user 及其全部 session 和下属数据 |

删除只写 `deleted_at`（各表原来的 `deleted` 布尔列已迁移掉）。同时补齐了文档里有、代码里没有的 `POST .../sessions`、`POST .../sessions/{id}/runs`（忽略 `library_ids`）、`GET .../runs/{id}`、`GET .../history`（`running` 时 409 `run_not_finished`）、嵌套 `interrupt`、嵌套 `DELETE .../files/{id}`。Playground 会话栏每项悬停出现 ×，确认后调用删除；聊天改走嵌套路由。LangGraph checkpoint（agent 对该会话的记忆）不在数据模型里，删会话不动它；会话已不可达，新 run 无法再用到它。

## deepagents 与 Python

deepagents 要求 Python ≥ 3.11，236 宿主 `python3` 是 3.10，所以用 uv 建了 `~/viknow3/venv312`（Python 3.12，`deepagents 0.7.29`、`langgraph 1.2.16`），`scripts/deploy-playground-ui.sh` 默认用它（可用 `VIKNOW_PYTHON` 覆盖），缺 deepagents 时拒绝启动。

## 236 清理记录（2026-10-21）

全部在 236 本机容器 `viknow3-ops-postgres`（:16432）和 JuiceFS S3 网关（`viknow-juicefs-s3-gateway`，bucket `viknow`）上进行，不涉及阿里云。`viknow_ops`（线上配置）未动。

| 库 | 删除的表 | 清空的数据 |
|----|----------|-----------|
| `viknow_access` | 旧模型单数表 `agent` `event` `file` `file_body` `profile` `run` `run_file` `runtime` `session` `user` `workspace`（代码已不用，仅互相外键） | `users` `sessions` `workspaces` `files` `run_refs`（17 个测试用户、35 会话、38 轮次等） |
| `viknow_agent` | schema `ai`（`agno_runs` `agno_sessions` `agno_schema_versions`） | checkpointer 表（保留 `checkpoint_migrations` 版本行） |
| `viknow_hyperrag` | `index_jobs`（代码已不用） | `knowledge_libraries` `knowledge_assets`（11 个测试库） |
| `*_test` | 同上的旧表 / `ai` / `index_jobs` | 测试残留行 |
| S3 `viknow` | — | 仅 viknow3 资产引用的 2 个对象（`fast-lib` 下的测试 PDF） |

浏览器里缓存了已删除用户时，Playground 页面会自动清掉缓存并回到登录页，重新创建用户即可。

## 验证

删除与事件存储（第二次）：
- 236 单测（venv312）：新增 `tests/test_session_lifecycle.py` 11 项全部通过；全量 51 项中只有下面 3 项既有失败。
- `:5198` 真实 Qwen + deepagents 冒烟 21 项全部通过：两轮记忆（第二轮答出「小周」）、`history` 与 SSE 逐条一致、运行中删会话 409、`history` 运行中 409、嵌套中断、缺 `user_id` 422、他人 404、删除 204 后各读接口 404；重启进程后另一会话的 `history`、run 状态和 SSE 回放仍在；库里该会话的 session/workspace/3 个 run/27 条 event 是同一个 `deleted_at`。浏览器：会话栏 × 删除、刷新后仍不在。测试数据已清空（`viknow_access`、`*_test`、checkpoint、`delete-demo` 库）。

方案 B（第一次）：

- 236 单测（`pg-test.env`）：`tests/test_agent_service.py` 16 项通过（连续 run、6 路并发库隔离、中断后同会话继续、会话记忆、终态只写一次、A2A、未知 runtime）。全量 40 项中 3 项失败为本次改动之前就存在的问题（见下）。
- `:5198` 真实 Qwen 冒烟：多轮记忆（第二轮答出「青松」）、3 路并发各自只看到自己的库（约 1.2s）、流式中断 0.01–0.17s 停止且同会话可继续、连续 3 轮完成；浏览器手测三轮对话 + 工具日志 + 会话列表。

## 遗留

1. 236 上缺 HyperRAG 运行依赖（`jieba`、私有包 `scheduler-core` 等），`manual-qa` 的真实 HyperRAG 检索不可用；对应单测在缺依赖时跳过。
2. 早于本次的 3 个失败单测：`test_access.test_modules`（会话忙检测已修好，现在停在 HyperRAG 检索：缺 `hyperrag` 模块）、`test_algorithm_ui`（页面文案）、`test_ops_config`（`agent_model` 存的是 ops 模型 id，如 `private:qwen`，`model_resolve` 还不认识，直接接回会把模型名发成 `qwen`）。
