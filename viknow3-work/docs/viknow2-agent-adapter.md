# QA 黑盒（viknow-v2 拷贝）

viknow3 **外层**负责会话、授权、事件、Postgres checkpointer（`thread_id = session_id`）、SSE 持久化。

**内层**在 `application/agent/qa_box/`：从 viknow2 拷贝的 viknow-v2 问答实现（deepagents 图、skills、tools、harness、workspace）。外层通过 `viknow2_adapter` 类型与 `qa_box.runtime.QaBoxRuntime.run_turn` 交互，无 runtime 切换开关。

| 层 | 路径 |
|----|------|
| 外壳入口 | `agentcore/runtime/host.py` → `qa_box/deepagents_runner.py` |
| 契约类型 | `application/agent/viknow2_adapter/types.py` |
| 黑盒实现 | `application/agent/qa_box/` |
| HyperRAG / Actio | `application/hyperrag/client.py`、`application/actio`（工具层调用，不直连 v2 access） |

多轮：每 run 只传入本轮用户消息；历史由 LangGraph checkpoint 在外层 DB 中按 `session_id` 累积，黑盒编译图时注入同一 checkpointer。
