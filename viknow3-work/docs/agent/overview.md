# Agent

一个 agent 是 `src/viknow3/agents/profiles/*.yaml` 里的一份 profile，加上共用的 `src/viknow3/agents/skills/`。它不是单独的进程，也不是容器。回合在 `src/viknow3/agents/runtimes/`：`langgraph` 用 DeepAgents，`adk` 用 ADK。外壳按 profile 的 `runtime` 字段选择适配器。

你用 Agent 做这些事：

- 按对外 id 列出已登记的 agent，并开始或继续它的会话。
- 把一轮用户消息交给 runtime，读回 SSE 事件和最终正文。
- 用 profile 声明这个 agent 能用哪些工具和技能。

## 核心概念

**Profile。** 一份 YAML。`id` 就是对外 id。现在登记的是 `expertbuddy-deepagents` 和 `expertbuddy-adk`。

**Agent 档案。** `src/viknow3/agents/profiles/*.yaml`。技能正文在 `src/viknow3/agents/skills/`，两份 profile 指向同一棵目录。

**Run。** 一轮调用。外壳生成 `run_id`，把 `host.run` 提交到进程内唯一的 agent loop。一个会话同时只能有一个 `running` 的 run。

**Session。** 外壳里的一行会话。它绑定 `user_id`、`agent_id`、`workspace_id` 和第一次写入的 `library_ids`。继续会话时不会改这组库。

**Runtime。** 真正执行「模型、工具、模型」的循环。`runtime: langgraph`（profile 没写这个字段时的缺省）走 `deepagents_adapter`。`runtime: adk` 走 `adk_adapter`。

Registry 把 profile 里的 `runtime` 原样记下来。`host.turn_runtime` 读这个字段。

## 已登记的 agent

`layout.discover_agents` 扫描 `src/viknow3/agents/profiles/*.yaml`。Registry 用 `public()` 对外只暴露 `id`、`label`、`description` 和时间戳。

| profile 文件 | 对外 id | runtime 字段 | 回合 |
| --- | --- | --- | --- |
| `profiles/expert_buddy.yaml` | `expertbuddy-deepagents` | `langgraph` | `runtimes/deepagents` |
| `profiles/expert_buddy_adk.yaml` | `expertbuddy-adk` | `adk` | `runtimes/adk` |

两个 agent 使用同一份 viknow2 线上技能 `knowledge-qa-v2`（`0.31.0-final-check-tool`）和 `visual-ground`。工具与线上 profile 一致：`knowledge_discover`、`uploaded_image_search`、`answer_plan`、`knowledge_search`、`video_inspect`、`visual_ground`、`document_inspect`、`list_video_segments`、`list_doc_segments`。最终答案由无参数 `final_check()` 提交。

`expertbuddy-deepagents` 仍先 `read_file` 读技能，并跑回答检查。`max_retries` 是 0，未通过时发布当前答案。`expertbuddy-adk` 把同一份技能写进指令，不读文件，也不跑回答检查。

## 外壳里的对象

| 对象 | 代码 | 作用 |
| --- | --- | --- |
| 入站 | `service/host/inbound/gateway.py` | 按 profile 找到 agent，拒绝未知 profile |
| 编排 | `service/host/environment/orchestrator.py` | 建 `RunContext`，把任务交给 agent loop，处理中断 |
| Host | `service/host/runtime/host.py` | 滤库、写开头事件、调用适配器 |
| 出站 | `service/host/outbound/gateway.py` | 这个用户是否被授予某个库 |
| 事件 | `service/host/runtime/events.py` | 把过程写成 `run_events` |
| 适配器 | `service/agent/deepagents_adapter/`、`service/agent/adk_adapter/` | 外壳类型和对应 runtime 之间的翻译 |

出站网关的授权是进程内集合：本轮提交时 `grant` 把这次的 `library_ids` 记到该用户名下。`host` 只把用户被授予、且本次请求里点名的库放进 `AgentTurnInput`。

## 下一步

- [Runtime](runtime.md)：循环、checkpoint，以及再接一个 runtime 时改哪里。
- [会话](sessions.md)：`run_events` 和 checkpoint 各记什么。
- [事件](events.md)：这一轮会写出哪些事件。
- [工具和模型](tools.md)：profile 里的工具，以及模型从哪里来。
- [HTTP](http.md)：路径、请求字段和错误码。
