# Agent 外壳

viknow3 外层负责会话、库授权、事件和 SSE。它把 runtime 的输出写成 events。

回合循环在 `src/agents/runtime`。某个 agent 的 profile 和 skills 在 `src/agents/<agent>/`。DeepAgents 自己把对话上下文写进 checkpoint。外壳只提供 agent 库的连接串。

| 层 | 路径 |
|----|------|
| 外壳入口 | `agentcore/runtime/host.py` |
| 契约与 `run_turn` | `application/agent/deepagents_adapter/` |
| 对话上下文 | `src/agents/runtime` 的 checkpointer。键由 runtime 自己定 |
| 回合循环 | `src/agents/runtime/` |
| HyperRAG / Actio | `application/hyperrag/client.py`、`application/actio` |

多轮：每 run 只传入本轮用户消息，以及 user、workspace、session。用户能看见的问答在 `run_events`。agent 下一轮要用的上下文由 runtime 自己的 checkpoint 累积。
