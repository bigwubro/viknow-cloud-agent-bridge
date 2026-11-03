# viknow2 Agent 黑盒适配层

viknow-platform **外层**只负责会话、授权、事件、持久化；**内层**应与 viknow2 `viknow-v2` agent **同一份实现**（未来以 Python 包依赖，而非在 `knowledge_assistant/` 里双份维护）。

## 代码位置

| 层 | 路径 |
|----|------|
| 适配类型与协议 | `src/viknow-platform/application/agent/viknow2_adapter/` |
| 过渡实现（当前） | `compat_runtime.py` → 仍调用 `knowledge_assistant/*` |
| 外层入口 | `agentcore/runtime/host.py` → `deepagents_runner.py` |
| 目标 viknow2 包（待接） | Gitea `castmeta-research/viknow2` 内 agent 运行时 |

环境变量：

- `VIKNOW_AGENT_RUNTIME=compat`（默认）：当前 viknow-platform 内嵌实现
- `VIKNOW_AGENT_RUNTIME=viknow2`：预留；安装 viknow2 agent wheel 后由 `get_runtime()` 切换

## 外层 ↔ 黑盒 契约

### 输入 `AgentTurnInput`

- `session_id` / `run_id` / `user_id`
- `user_message`
- `library_ids`（Playground run 授权）
- `model_selector`（可选）

Host 在调用前已 `bind_grant()`；黑盒通过 `current_grant()` / `granted_library_ids()` 读库范围。

### 输出 `AgentTurnResult`

- `output` / `citations` / `token_usage`
- 流式过程经 `RunEvents`（`tool_call_*`、`message_delta`、`run_completed`）

### 依赖注入 `AgentRuntimeDeps`（演进）

- `library_allowed` / `emit_event`
- 将来：`hyperrag_factory` / `actio_factory` 由 viknow-platform 注入，viknow2 包不直连 viknow-platform access

## viknow2 侧建议导出的最小面（单包 `viknow-agent-runtime`）

从 viknow2 仓抽出或 re-export，**不要**让 viknow-platform 再拷贝这些文件：

| 符号 / 模块 | 作用 |
|-------------|------|
| `config/profiles/viknow-v2.yaml` + loader | profile、enabled_tools/skills、answer_review |
| `viknow.core.tools.registry.build_viknow_tool_specs` | ToolSpec / JSON schema |
| `viknow.agent_adapters.deepagents_tools.build_deepagents_tools` | LangChain tools |
| DeepAgents 图构建（`create_deep_agent` 封装） | 与 v2 Playground 相同图 |
| Harness：`BeforeToolCall`、skill reminder、citation 登记 | 与 v2 一致的门禁 |
| Skills 目录 `knowledge-qa-v2`、`visual-ground` | 全文同步，非摘要 |
| `run_turn(session_id, message, grant, checkpointer, emit)` | **唯一运行时入口** |

viknow-platform 适配器只做：

```text
RunState + RunEvents  →  AgentTurnInput + deps  →  viknow2.run_turn  →  AgentTurnResult
```

## 事件映射（viknow-platform SSE 已对齐）

| viknow2 / LangGraph | viknow-platform `run_events.type` |
|---------------------|---------------------------|
| user message | `user_message` |
| model start | `model_call_started` |
| token stream | `message_delta` |
| tool start/end | `tool_call_started` / `tool_call_completed` |
| final message | `message_completed` |
| run end | `run_completed` / `run_failed` |
| answer review | `answer_review_started` / `answer_review_completed` |

契约测试：同一 `library_ids` + 固定 mock HyperRAG fixture，对比 tool 序列与返回 JSON 字段。

## 迁移阶段

1. **现在**：`Viknow2CompatRuntime` + 文档；`deepagents_runner` 走适配器（行为不变）。
2. **viknow2 抽包**：在 viknow2 增加 `run_turn()`，版本 tag；viknow-platform `pip install` 该包。
3. **删重复代码**：移除 viknow-platform 内与 v2 重复的 `tools.py` / harness 副本，只保留 adapter + 注入。
4. **CI**：viknow2 agent 单测 + viknow-platform `test_agent_service` + 236 `scripts/test_deepagents_qa.py`。

## 与索引 / Actio 的关系

- 黑盒 **不** 包含 HyperRAG 索引管线；索引仍在 viknow-platform access + HyperRAG。
- 黑盒 **使用** 已注入的 HyperRAG/Actio client 做 `knowledge_search` / inspect。
- Actio 版本在部署层 pin（如 `castmeta-research/actio@main`），与 agent 包版本独立发布。
