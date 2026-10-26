# Agent

一个 agent 是 `src/agents/<目录>/profile/*.yaml` 里的一份 profile，加上同目录的 `skills/`。它不是单独的进程，也不是容器。多个 agent 共用 `src/agents/runtime` 里的回合实现，并由外壳按对外 id 选中。

你用 Agent 做这些事：

- 按对外 id 列出已登记的 agent，并开始或继续它的会话。
- 把一轮用户消息交给 runtime，读回 SSE 事件和最终正文。
- 用 profile 声明这个 agent 能用哪些工具和技能。

## 核心概念

**Profile。** 一份 YAML。`id` 是 profile id。Playground 使用的对外 id 可能不同。`expert_buddy` 的 profile id 是 `expert_buddy`，`layout.py` 把它的对外 id 写成 `deepagents`。

**Agent 包。** `src/agents` 下的一个目录，里面有 `profile/*.yaml`。目录名 `runtime` 会被跳过，它是共用回合，不是 agent。

**Run。** 一轮调用。外壳生成 `run_id`，把 `host.run` 提交到进程内唯一的 agent loop。一个会话同时只能有一个 `running` 的 run。

**Session。** 外壳里的一行会话。它绑定 `user_id`、`agent_id`、`workspace_id` 和第一次写入的 `library_ids`。继续会话时不会改这组库。

**Runtime。** 真正执行「模型、工具、模型」的循环。当前接通的实现是 DeepAgents，入口是 `deepagents_adapter`。

Registry 把每个包的 `runtime` 字段写成字面量 `langgraph`。`host` 不读这个字段，一律调用 `deepagents_adapter.get_runtime()`。

## 已登记的 agent

`layout.discover_agents` 扫描带 profile 的目录。Registry 用 `public()` 对外只暴露 `id`、`label`、`description` 和时间戳。

| 目录 | 对外 id | profile id | 名称 |
| --- | --- | --- | --- |
| `expert_buddy` | `deepagents` | `expert_buddy` | 知识助手 |
| `evidence_oracle` | `evidence-oracle` | `evidence-oracle` | 证据采集器 |
| `doc_eval` | `mmlongbench-doc-eval` | `mmlongbench-doc-eval` | MMLongBench-Doc 评测助手 |
| `doc_eval` | `mmlongbench-doc-extract` | `mmlongbench-doc-extract` | MMLongBench-Doc 答案抽取器 |
| `ops` | `ops` | `ops` | 运维助手 |
| `video_buddy` | `viknow-v2-advanced-video` | `viknow-v2-advanced-video` | 知识助手（高级视频实验） |

工具和技能以各 profile 的 `enabled_tools`、`enabled_skills` 为准。知识助手启用 `knowledge_discover`、`knowledge_search`、`query_refine`、`answer_plan`、`uploaded_image_search`、`video_inspect_2_0`、`visual_ground`、`document_inspect`、`list_video_segments`、`list_doc_segments`，技能是 `knowledge-qa-v2` 和 `visual-ground`。它的 `answer_review.max_retries` 是 0：检查仍会跑，未通过时发布当前答案。

## 外壳里的对象

| 对象 | 代码 | 作用 |
| --- | --- | --- |
| 入站 | `application/agentcore/inbound/gateway.py` | 按 profile 找到 agent，拒绝未知 profile |
| 编排 | `application/agentcore/environment/orchestrator.py` | 建 `RunContext`，把任务交给 agent loop，处理中断 |
| Host | `application/agentcore/runtime/host.py` | 滤库、写开头事件、调用适配器 |
| 出站 | `application/agentcore/outbound/gateway.py` | 这个用户是否被授予某个库 |
| 事件 | `application/agentcore/runtime/events.py` | 把过程写成 `run_events` |
| 适配器 | `application/agent/deepagents_adapter/` | 外壳类型和 DeepAgents 之间的翻译 |

出站网关的授权是进程内集合：本轮提交时 `grant` 把这次的 `library_ids` 记到该用户名下。`host` 只把用户被授予、且本次请求里点名的库放进 `AgentTurnInput`。

## 下一步

- [Runtime](runtime.md)：循环、checkpoint，以及再接一个 runtime 时改哪里。
- [会话](sessions.md)：`run_events` 和 checkpoint 各记什么。
- [事件](events.md)：这一轮会写出哪些事件。
- [工具和模型](tools.md)：profile 里的工具，以及模型从哪里来。
- [HTTP](http.md)：路径、请求字段和错误码。
