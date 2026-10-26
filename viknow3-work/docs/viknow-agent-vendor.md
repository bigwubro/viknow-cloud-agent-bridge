# Expert Buddy

问答 agent 放在 `src/agents/expert_buddy`。外壳留在 `src/viknow3`。

## 原则

- **`src/agents/expert_buddy/`**：知识问答本身。profile、`knowledge-qa-v2` 与 `visual-ground`、tool gate、stop gate、证据账本、答案复核、`qa_plan_middleware`、勾选的 tools、bubblewrap 沙箱、单回合执行器。
- **`src/viknow3/`**：会话和 run、SSE、`bind_grant` / `library_ids`、Postgres checkpointer（`thread_id = session_id`）、HyperRAG client、Actio、ops 模型配置。expert_buddy 只接收这些。
- 飞书、ViCoach、viknow2 HTTP API、JuiceFS、索引流水线、eval/learning，以及 `qa_box` 的 builder/tools/harness，没有留在这棵树里。
- 回合预算在 profile 里关闭。长期记忆、Langfuse、Hermes、Redis 调度、License 配置仍被回合模块引用，Playground 不启用它们。回合循环还是 `DeepAgentsRuntime` 这一个模块，沙箱和 `qa_plan_middleware` 从这里调用。

## 目录

| 路径 | 放什么 |
|------|------|
| `src/agents/expert_buddy/profile/expert_buddy.yaml` | 知识助手 profile。回合预算关闭 |
| `src/agents/expert_buddy/skills/` | `knowledge-qa-v2`、`visual-ground`（不含 maintenance） |
| `src/agents/expert_buddy/agent_adapters/`、`core/` | 问答回合、harness、tools、沙箱 |
| `src/viknow3/application/agent/viknow2_adapter/` | 外壳契约 `AgentTurnInput` / `run_turn` |
| `src/viknow3/application/agent/qa_box/` | 只留 `deepagents_runner.py` 和 runtime 委托 |

## 同步

不要再把 viknow2 整包同步成 `src/viknow`。问答改动直接落在 `src/agents/expert_buddy`。

## 边界改造（只动接线，不改编排）

1. **HyperRAG / 索引查询**：viknow2 工具内进程内 adapter → 注入 `viknow3.application.hyperrag.client.HyperragClient`（或 shim 实现 viknow2 工具期望的 surface）。
2. **Actio**：同上，走 `viknow3.application.actio`。
3. **Grant / library allowlist**：外壳 `bind_grant()`；viknow2 `ToolRuntime` / allowlist 从注入上下文读取。
4. **多轮记忆**：外壳 Postgres checkpointer + `thread_id=session_id`；viknow2 `DeepAgentsRuntime` 的 checkpointer 配置需与 viknow3 **共用同一 saver**（接线项，未改 viknow 内部算法）。
5. **模型**：viknow3 ops 解析 → 填入 viknow2 `AgentModelConfig` / factory。

## 接线目标

外壳 `run_turn` 委托 `agents.expert_buddy.agent_adapters.deepagents.DeepAgentsRuntime`，事件映射到 `RunEvents`。

当前状态：Playground 走 expert_buddy。共用 `agentcore/runtime/checkpoint`，`thread_id=session_id`。profile 不启用回合预算。`src/viknow` 整包和 `qa_box` 的第二套实现已从这棵树拿掉。
