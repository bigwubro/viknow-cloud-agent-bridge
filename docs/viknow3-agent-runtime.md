# viknow3 Agent 运行时（方案 B：去掉 Agno）

业务仓：Gitea `castmeta-research/viknow3`，分支 `agent/viknow-v3-knowledge-mvp`（236：`/home/cursor-agent/src/viknow3`，Playground `:5198`）。

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

Agent：`viknow-v3`（deepagents 已装时 `create_deep_agent`，否则 `create_react_agent`，提示词与工具取自 viknow-v2 profile）、`manual-qa`（`knowledge_search` + 真实模型）、`langgraph-qa` / `other-agent`（回显图）、`deepagents-demo`。`VIKNOW_USE_SCRIPT_MODEL=1` 只在测试里换脚本模型。

模型：`/api/v1/agent/models` 第一项是 `server-default`（标注实际模型 `vllm/Qwen/Qwen3.6-35B-A3B-FP8`，即 236 私网 Qwen3.6），Playground 默认选它；此前默认落到 SiliconFlow。

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

- 236 单测（`pg-test.env`）：`tests/test_agent_service.py` 16 项通过（连续 run、6 路并发库隔离、中断后同会话继续、会话记忆、终态只写一次、A2A、未知 runtime）。全量 40 项中 3 项失败为本次改动之前就存在的问题（见下）。
- `:5198` 真实 Qwen 冒烟：多轮记忆（第二轮答出「青松」）、3 路并发各自只看到自己的库（约 1.2s）、流式中断 0.01–0.17s 停止且同会话可继续、连续 3 轮完成；浏览器手测三轮对话 + 工具日志 + 会话列表。

## 遗留

1. 236 宿主 Python 没装 `deepagents`，`viknow-v3` 实际走 ReAct 回退（技能文件未被读取）；需要装 `viknow3[deepagents]` 才是 viknow-v2 同栈。
2. 236 宿主 Python 缺 HyperRAG 运行依赖（`jieba`、私有包 `scheduler-core` 等），`manual-qa` 的真实 HyperRAG 检索不可用；对应单测在缺依赖时跳过。
3. 早于本次的 3 个失败单测：`test_access.test_modules`（会话忙检测被"必须选库"校验挡住）、`test_algorithm_ui`（页面文案）、`test_ops_config`（`agent_model` 存的是 ops 模型 id，如 `private:qwen`，`model_resolve` 还不认识，直接接回会把模型名发成 `qwen`）。
4. run 事件仍只在内存里（`EventSink`），进程重启后历史轮次只能从 `run_refs` 读到输入/输出，读不到逐条事件。
