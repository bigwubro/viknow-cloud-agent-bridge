# Agent Runtime

Agent Runtime 是一轮调用的执行面。外壳负责接收请求和写事件。runtime 负责模型、工具、闸门、沙箱，以及它自己的对话上下文。

当前接通两个 runtime，外壳都通过 `run_turn` 调用。DeepAgents 在 `src/agents/runtime`，适配器在 `src/viknow3/application/agent/deepagents_adapter/`。ADK 在 `src/agents/adk_buddy`，适配器在 `src/viknow3/application/agent/adk_adapter/`。两边的 profile、技能和回合代码互不导入。

`host.turn_runtime` 读 Registry 里的 `runtime` 字段。profile 写了 `runtime: adk` 时走 ADK，其余走 DeepAgents。

你可以用这层做这些事：

- 在一个已经授权的库范围内跑完一轮工具循环。
- 把 runtime 的输出映射成外壳事件。
- DeepAgents 用 LangGraph checkpoint 记住这个 user、workspace、session 上的上下文。ADK 用自己的 session 记住同一组标识。

## 分层

```mermaid
flowchart TB
  caller["调用方"]
  access["接入层 access/agent"]
  shell["外壳 agentcore"]
  events["run_events"]
  adapter["deepagents_adapter"]
  runtime["src/agents/runtime"]
  pkg["agent 包 profile + skills"]
  ckpt["LangGraph checkpoint"]
  ops["viknow_ops"]
  engine["HyperRAG 引擎"]

  caller --> access --> shell
  shell --> events
  shell --> adapter --> runtime
  runtime --> pkg
  runtime --> ckpt
  ops --> adapter
  shell --> engine
  shell -->|"装配时注入 KnowledgeClient 和 ActioClient"| adapter
```

依赖只有一个方向。`src/agents` 里没有 `import viknow3`，也没有 `import hyperrag` 或 `import actio`。知识库引擎由外壳创建，适配器在 `get_deepagents_runtime` 里调用 `get_engine_adapter()`，把得到的对象当作 `KnowledgeClient` 传给 `build_agent_runtime`。同一个调用把 `get_actio_client()` 当作 `ActioClient` 传进去。

## 一轮调用

`Orchestrator.start` 生成 `run_id`，把 `host.run` 提交到名为 `viknow-agent-loop` 的常驻 asyncio 循环。这个循环在守护线程上。同步地堵在这条线程里的调用会挡住其他轮次。

```mermaid
sequenceDiagram
  participant Caller as 调用方
  participant Access as 接入层
  participant Host as host
  participant Events as run_events
  participant Adapter as deepagents_adapter
  participant Runtime as DeepAgents runtime
  participant Ckpt as Checkpoint
  participant Model as 平台模型
  participant RAG as HyperRAG

  Caller->>Access: 本轮消息、user、session、library_ids
  Access->>Host: agent loop 上执行 host.run
  Host->>Host: 滤掉这个用户不能搜的库
  Host->>Events: run_started、user_message
  Host->>Adapter: run_turn
  Adapter->>Runtime: submit
  Runtime->>Ckpt: 按 user、workspace、session 读取
  loop 直到本轮结束
    Runtime->>Model: 生成下一步
    Model-->>Runtime: 正文或工具调用
    opt 调用知识库工具
      Runtime->>RAG: KnowledgeClient 检索
      RAG-->>Runtime: 证据
    end
    Runtime-->>Adapter: runtime 事件
    Adapter->>Events: message_delta、tool_call、answer_review
  end
  Adapter->>Events: message_completed、run_completed
  Events-->>Caller: SSE 读同一张 run_events
```

`host` 传给适配器的 `AgentTurnInput` 含有 `session_id`、`run_id`、`user_id`、`user_message`、滤过的 `library_ids`、模型名，以及会话行上的 `workspace_id`。没有会话行时 `workspace_id` 为空，checkpoint 键里的 workspace 段用 `default`。

适配器按对外 id 找到 agent 包，每个包装配一次 `DeepAgentsRuntime` 并缓存。`submit` 之后，适配器从 runtime 拉事件，用 `apply_vendor_event` 写成外壳事件。runtime 自己的 `RUN_STARTED` 和 `USER_MESSAGE` 被丢掉，因为外壳已经写过。

## 外壳和 runtime 各持有什么

| 外壳 | runtime |
| --- | --- |
| HTTP、app key、会话和 run | 一轮里的模型、工具、闸门、回答检查、沙箱 |
| `run_events`，以及 run 上的正文、引用、token | LangGraph checkpoint |
| 三个库的连接串 | 用 `VIKNOW_AGENT_DSN` 自己建 checkpointer |
| 平台模型端点 | `runtime.yaml` 里的温度、超时、沙箱、递归上限 |
| HyperRAG 引擎、Actio 包 | `KnowledgeClient` 和 `ActioClient` 上的方法 |
| 这一轮允许的库 id | thread id：`{user_id}:{workspace_id 或 default}:{session_id}` |

外壳不创建 `AsyncPostgresSaver`，也不读 checkpoint 的 `channel_values`。只按旧的 `session_id` 存下来的 checkpoint 对不上这个 thread id，下一轮不会续上。

下一轮调用图之前，runtime 先读这条 thread。没有 tool 结果的调用会补一条 `tool call did not finish (run interrupted)`，再附上这一轮的用户消息。

对话模型、监控和 Mem0 的调用留在 runtime。知识库和 Actio 不同：包由外壳持有，runtime 只使用注入的 client。

`runtime.yaml` 里当前的沙箱是 bubblewrap，虚拟工作区，单文件 25MB，内存 512MB，无网络。模型块只保留温度 0.7、重复惩罚 1.0、最大输入 156000、超时 600 秒。地址和密钥不在这个文件里。

## 再接一个 runtime

HTTP、会话、`run_events` 和库授权不用改。ADK 已经按这个接法落地：

1. `src/agents/adk_buddy` 自己维护 profile、技能和回合。session 使用外壳传入的 `user_id`、`workspace_id`、`session_id`，写在 ADK 自己的 session 里。外壳不解析这份 session。
2. `adk_adapter` 实现 `run_turn(turn, state, events)`，把输出写成同一组事件。检索使用外壳注入的知识库 client。ADK 不调用 Actio，也没有 DeepAgents 的检查、闸门和沙箱。

```mermaid
sequenceDiagram
  participant Host as host
  participant DA as deepagents_adapter
  participant ADK as adk_adapter
  participant Events as run_events
  participant Ckpt as LangGraph checkpoint
  participant Sess as ADK session

  Host->>Host: 读 profile.runtime
  alt runtime 不是 adk
    Host->>DA: run_turn
    DA->>Ckpt: 自己的 thread id
    DA->>Events: 同一组事件
  else runtime 是 adk
    Host->>ADK: run_turn
    ADK->>Sess: 自己的 session
    ADK->>Events: 同一组事件
  end
```

两边的对外 id 分别是 `expertbuddy-deepagents` 和 `expertbuddy-adk`。技能正文是同一份 `knowledge-qa`，都只引用 `chunk_text`。DeepAgents 用 `read_file` 读取它，ADK 把它附在指令里。

## 下一步

- [会话](sessions.md)
- [事件](events.md)
- [工具和模型](tools.md)
- [评测](adk-eval.md)：同一知识库、同一组问题上 DeepAgents 和 ADK 的对照。
