# Agent 逻辑视图

这篇按当前代码说明 ViKnow 3 的 agent 是怎么分层的。文档站 [逻辑视图](http://36.103.198.236:5208/design/logical-view.html) 写的是整体规划；这里只写仓库里已经接通的那条链路。HTTP 的字段和表结构仍以文档站为准：对话见 [runs](http://36.103.198.236:5208/start/api/runs.html)，会话表见 [session](http://36.103.198.236:5208/model/session.html)。

依赖只有一个方向：外壳调用 `src/agents`。`src/agents` 不引用 `viknow3`。

## 逻辑元素

| 元素 | 代码 | 对外做什么 |
| --- | --- | --- |
| 接入层 | `src/viknow3/access/agent/` | `/api/v1/agent`。校验 app key，聚合成一次会话或一轮调用 |
| Agent 外壳 | `src/viknow3/application/agentcore/` | 建 run、授权可搜的库、把过程写成 `run_events`、用 SSE 推回去 |
| Runtime 适配器 | `src/viknow3/application/agent/deepagents_adapter/` | 把外壳的一轮输入交给某个 runtime，再把 runtime 的输出写成外壳事件 |
| 共用回合 | `src/agents/runtime/` | 模型、工具、闸门、沙箱，以及这个 runtime 自己的对话上下文 |
| Agent 包 | `src/agents/<agent>/` | 一份 profile 和它的 skills。目录名 `runtime` 不是 agent |
| 知识库 | `src/viknow3/application/hyperrag/` | 外壳持有引擎。runtime 只拿到注入的 `KnowledgeClient` |
| 平台配置 | `viknow_ops`，经 `src/viknow3/ops/` | 模型名、provider 地址和密钥的环境变量名 |

Playground 上的 `deepagents` 就是目录 `expert_buddy`。profile 文件里的 id 是 `expert_buddy`，对外 id 由 `src/agents/layout.py` 写成 `deepagents`。

## 分层

```text
接入层  access/agent
    │
    ▼
外壳    agentcore：Gateway → Orchestrator → host
    │
    ▼
适配器  deepagents_adapter.TurnRuntime
    │
    ▼
runtime src/agents/runtime
    │
    ├── agent 包：profile + skills
    ├── 平台模型：viknow_ops
    └── 知识库：外壳注入的 KnowledgeClient
```

| 层 | 职责 | 详细位置 |
| --- | --- | --- |
| 接入层 | 向上提供 HTTP，向下只调用 Agent client。不运行模型，不写 checkpoint | `access/agent/routes.py`、`access/agent/service.py` |
| 外壳 | 一次调用的生命周期：会话、run、库授权、事件、中断 | `application/agentcore/` |
| 适配器 | 外壳类型和某一个 runtime 之间的翻译。现在只有 DeepAgents | `application/agent/deepagents_adapter/` |
| runtime | 一轮里的循环，以及下一轮还要用的 agent 上下文 | `src/agents/runtime/` |
| agent 包 | 这个 agent 启用哪些工具、哪份技能、回答检查怎么配 | `src/agents/<agent>/profile/*.yaml`、`skills/` |

外壳看 runtime，只看 `TurnRuntime.run_turn`。输入是 `AgentTurnInput`，输出是 `AgentTurnResult`，过程通过 `RunEvents` 往外写。

## 一轮调用

`Orchestrator.start` 生成 `run_id`，把 `host.run` 提交到进程里唯一的 agent loop。

1. 接入层收下 `message`、`user_id`、`session_id`、`library_ids` 和可选的模型名。
2. `host` 用出站网关滤掉这个用户不能搜的库。
3. 会话行里有 `workspace_id` 就放进 `AgentTurnInput`；没有会话行时，这一项为空。
4. `host` 先写 `run_started` 和 `user_message`。
5. `deepagents_adapter` 按对外 id 找到 agent 包，调用 `DeepAgentsRuntime.submit`。
6. 适配器把 runtime 事件写成 `message_delta`、`tool_call_started`、`tool_call_completed`、`answer_review_started`、`answer_review_completed`。结束时调用 `RunEvents.completed`，写出 `message_completed` 和 `run_completed`。失败和中断分别是 `run_failed`、`run_interrupted`。
7. `EventSink` 把事件插入 `run_events`。SSE 和历史读的是同一张表。

用户能回放的问答来自 `run_events` 里的 `user_message` 和 `message_completed`（`Store.session_turns`）。外壳不从 checkpoint 里拆消息。

## 内外边界

| 外壳持有 | runtime 持有 |
| --- | --- |
| HTTP、app key、会话和 run | 一轮里的模型调用、工具、闸门、回答检查、沙箱 |
| `run_events`，以及 run 上的正文、引用、token | DeepAgents 的 LangGraph checkpoint |
| 三个库的连接：`VIKNOW_ACCESS_DSN`、`VIKNOW_AGENT_DSN`、`VIKNOW_HYPERRAG_DSN` | 用 `VIKNOW_AGENT_DSN` 自己建 checkpointer。外壳不创建 `AsyncPostgresSaver`，也不读 `channel_values` |
| 平台模型端点 | `runtime.yaml` 里的循环参数：温度、超时、沙箱、递归上限 |
| HyperRAG 引擎 | `KnowledgeClient`：发现、检索、片段、证据。工具不进引擎私有方法 |
| 这一轮允许的 `library_ids` | checkpoint 的 thread id：`{user_id}:{workspace_id 或 default}:{session_id}` |

只按旧的 `session_id` 存下来的 checkpoint 对不上这个 thread id，下一轮不会续上。

对话模型、监控、Mem0、视频发现的 client 留在 runtime。它们是一轮内部对外部服务的调用。知识库不同：引擎是长生命周期的平台能力，由外壳创建后注入。

## 配置

外壳的 `build_agent_app_config` 组装 runtime 还需要的 `AppConfig`。它不读 `app.yaml`。

| 内容 | 来源 |
| --- | --- |
| 模型名和 provider | `viknow_ops` |
| 循环、沙箱、checkpoint 开关 | `src/agents/runtime/runtime.yaml` |
| 某个 agent 的工具、技能、回答检查 | `src/agents/<agent>/profile/*.yaml` |
| checkpoint 连接 | `VIKNOW_AGENT_DSN`，后端固定为 Postgres |
| 状态目录 | 外壳选定的 `files_root/state`。工具恢复日志写到这个目录下的 `traces/` |

## 现有 agent

`layout.discover_agents` 扫描 `src/agents` 下带 `profile/*.yaml` 的目录。

| 目录 | 对外 id | 名称 |
| --- | --- | --- |
| `expert_buddy` | `deepagents` | 知识助手 |
| `evidence_oracle` | `evidence-oracle` | 证据采集器 |
| `doc_eval` | `mmlongbench-doc-eval` | MMLongBench-Doc 评测助手 |
| `doc_eval` | `mmlongbench-doc-extract` | MMLongBench-Doc 答案抽取器 |
| `ops` | `ops` | 运维助手 |
| `video_buddy` | `viknow-v2-advanced-video` | 知识助手（高级视频实验） |

Registry 里这些 profile 的 `runtime` 字段目前都写成 `langgraph`。`host` 不读这个字段，一律调用 `deepagents_adapter.get_runtime()`。

## 再接一个 runtime

再接 Google ADK 时，外壳的 HTTP、会话、`run_events` 和库授权保持不变。新增两块：

1. ADK runtime 实现自己的循环和自己的 session。session 的键用外壳传入的 `user_id`、`workspace_id`、`session_id`，外壳不解析 ADK 的 session 状态。
2. 外壳里的 `adk_adapter`，实现同一个 `TurnRuntime.run_turn(turn, state, events)`。它把 ADK 的输出写成上面那组事件，需要检索时使用外壳已经建好的 `KnowledgeClient`。

`host.py` 里现在只有一处写死了 DeepAgents。这里按 agent 选择适配器。DeepAgents 的 `runtime.yaml`、LangGraph checkpoint、工具闸门和回答检查留在 `deepagents_adapter` 后面，ADK 不复用它们。

ADK runtime 同样不能引用 `viknow3`。适配器放在外壳，因为它翻译的是外壳的 `AgentTurnInput` 和 `RunEvents`。
