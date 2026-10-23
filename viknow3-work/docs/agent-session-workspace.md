# 生产级：每 Session 工作区（ADK + DeepAgents）

## 目标

- **一个 `session_id` 一块隔离工作区**，两种 runtime 共用同一套宿主机目录与虚拟路径约定。
- **Access 上传** → 物化到 `uploads/`；**run 产物** → `outputs/`、`scratch/` 可跨轮恢复。
- **DeepAgents** 继续在 bubblewrap 内映射为 `/workspace/...`；**ADK** 用同目录作 `LocalEnvironment` 执行根，skill 从会话 `.skills/` 加载。

## 目录布局（宿主机）

根路径：`{VIKNOW_AGENT_WORKSPACE_ROOT}/sessions/{session_id}/workspace/`

| 子目录 | 虚拟路径 | 用途 |
|--------|----------|------|
| `uploads/` | `/workspace/uploads/` | API 上传物化 |
| `scratch/` | `/workspace/scratch/` | 临时计算 |
| `outputs/` | `/workspace/outputs/` | 可交付产物 |
| `.skills/` | `/workspace/.skills/` | 本轮 profile 技能副本（run 结束删除） |

配置项沿用 `agent.deepagents.workspace`（`max_file_size_mb`、`staging.purge_*`），**两种 runtime 共用**。

## 生命周期（代码：`workspace_lifecycle.py`）

```text
prepare_session_run(session_id, enabled_skills)
  → mkdir、stage_runtime_skills、materialize_session_uploads
  → 从 access DB 恢复上轮 workspace_run_artifact（outputs/scratch）

[ 执行 agent 一轮 ]

finalize_session_run(session_id)
  → 将 outputs/scratch 写入 access.files（kind=workspace_run_artifact）
  → 删除 .skills/
  → 按配置 purge scratch/outputs 宿主机缓存
```

**DeepAgents（bubblewrap）** 在 `prepare` 之后额外：`restore_bubblewrap_workspace` → 跑图 → `capture_bubblewrap_workspace`（沙箱 download 合并到宿主机）→ `finalize_session_run`。

**ADK**：`prepare` → `build_llm_agent(session_workspace_root=..., session_skills_dir=.skills)` → `Runner.run_async` → `finalize`。

## 提示词策略（`workspace_runtime_instruction`）

有授权 `library_id` 时统一追加：

- 必须先 `knowledge_discover` / `knowledge_search`（及 skill 要求的 inspect）。
- **禁止**用 `glob`/`grep`/`ls` 在 workspace 猜知识库答案。
- DeepAgents：首工具 `read_file` `knowledge-qa-v2/SKILL.md`。
- ADK：先 `load_skill("knowledge-qa-v2")`。

## 安全边界

| Runtime | 文件/执行隔离 |
|---------|----------------|
| DeepAgents | 整轮 `deepagents_sandbox` + bubblewrap（生产默认） |
| ADK | 会话目录在宿主机；**`run_skill_script` / `execute`** 默认 **ADK `LocalEnvironment`**（`script_sandbox: host`） |

可选 `script_sandbox: bubblewrap` 仅对脚本执行启用 `BubblewrapScriptEnvironment`（一般内网 Playground 不需要）。

## 运维

- 磁盘：按 session 配额与定期清理 `sessions/*/workspace`（保留 access 元数据即可重建 uploads）。
- 观测：run 日志带 `session_id`；`finalize` 返回写入 artifact 数量。
- 236 Playground：`VIKNOW_AGENT_WORKSPACE_ROOT` 已在 `deploy-playground-ui.sh` 设置。

## 会话消息（`session_messages`）

- `session_messages("deepagents", session_id)`：LangGraph Postgres checkpointer 中的 `messages`。
- `session_messages("adk", session_id)`：ADK `adk` schema 中该 session 的 events，转换为 LangChain `HumanMessage` / `AIMessage` / `ToolMessage`（需 access 库中存在该 `session_id` 以解析 `user_id`）。

## 与 Access API 的关系

- `POST …/sessions/{id}/files` → `STORE.files`（用户上传）。
- `prepare` 物化到 `uploads/`。
- `finalize` 持久化 `workspace_run_artifact`（非用户上传 kind，供下轮 restore）。
