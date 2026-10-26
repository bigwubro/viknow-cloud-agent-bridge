# Agent 外壳

viknow3 外层负责会话、库授权、事件、Postgres checkpointer（`thread_id = session_id`）、SSE 持久化。

回合循环在 `src/agents/runtime`。某个 agent 的 profile 和 skills 在 `src/agents/<agent>/`。外壳按这次 run 的 profile id 取对应的 `DeepAgentsRuntime`。

| 层 | 路径 |
|----|------|
| 外壳入口 | `agentcore/runtime/host.py` |
| 契约与 `run_turn` | `application/agent/deepagents_adapter/` |
| 会话记忆 | `agentcore/runtime/checkpoint.py`，agent 库里的 LangGraph checkpoint。`thread_id` 是 `session_id` |
| 回合循环 | `src/agents/runtime/` |
| HyperRAG / Actio | `application/hyperrag/client.py`、`application/actio` |

多轮：每 run 只传入本轮用户消息。历史由 LangGraph checkpoint 在外层 DB 中按 `session_id` 累积。
