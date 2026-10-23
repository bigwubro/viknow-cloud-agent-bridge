# viknow2 问答 Agent 原样迁入（vendor）

## 原则

- **`src/viknow/`**：从 viknow2 `main` **整包同步**的 agent 实现（`import viknow.*` 路径不变），供对方团队在此仓继续开发。
- **`src/viknow3/`**：仅接入层（session/run、grant、SSE、checkpoint、HyperRAG/Actio **client**）。
- **禁止**在 viknow3 里再维护一套「读懂逻辑后的简化版」（如合并版 `QaHarnessMiddleware`）；过渡代码在接线完成后删除。

## 目录

| 路径 | 来源 |
|------|------|
| `src/viknow/` | viknow2 `src/viknow`（含 `agent_adapters/deepagents.py`、`core/tools/*`、runtime 等） |
| `config/profiles/` | viknow2 `config/profiles`（如 `viknow-v2.yaml`） |
| `src/viknow3/application/agent/viknow2_adapter/` | 外壳契约 `AgentTurnInput` / `run_turn` |
| `src/viknow3/application/agent/qa_box/` | **过渡**；接线完成后仅保留 `deepagents_runner.py` 或删尽 |

## 同步

```bash
# 236 上
VIKNOW2_SRC=/home/cursor-agent/work/viknow2 VIKNOW2_REV=main \
  bash scripts/sync-viknow2-agent-vendor.sh
```

## 边界改造（只动接线，不改编排）

1. **HyperRAG / 索引查询**：viknow2 工具内进程内 adapter → 注入 `viknow3.application.hyperrag.client.HyperragClient`（或 shim 实现 viknow2 工具期望的 surface）。
2. **Actio**：同上，走 `viknow3.application.actio`。
3. **Grant / library allowlist**：外壳 `bind_grant()`；viknow2 `ToolRuntime` / allowlist 从注入上下文读取。
4. **多轮记忆**：外壳 Postgres checkpointer + `thread_id=session_id`；viknow2 `DeepAgentsRuntime` 的 checkpointer 配置需与 viknow3 **共用同一 saver**（接线项，未改 viknow 内部算法）。
5. **模型**：viknow3 ops 解析 → 填入 viknow2 `AgentModelConfig` / factory。

## 接线目标

外壳 `QaBoxRuntime.run_turn` → 委托 **`viknow.agent_adapters.deepagents.DeepAgentsRuntime`**（或 viknow2 对外统一的 `AgentRuntime` 入口），事件映射到 `RunEvents`。

当前状态：Playground `run_turn` 已委托 `DeepAgentsRuntime`（`viknow2_adapter/vendor*`）；共用 `agentcore/runtime/checkpoint`；`thread_id=session_id`。`qa_box` 中 builder/tools/harness 仍待删除。
