# viknow-platform 知识助手与 viknow2 `viknow-v2` 对齐共识

## 意图（产品 / 架构）

在 **viknow-platform 单仓** 内，把 Playground / Agent Service 上的知识助手做成与 viknow2 **`viknow-v2` profile 一比一对齐** 的 agent：

- **对齐**：profile 元数据、DeepAgents 运行时形态、**enabled_tools / enabled_skills**、**system_prompt**、skill 工作流（含 `read_file` + SKILL.md）、工具 **id / 入参 schema / 返回 JSON 形态**、会话与 run/events（SSE）行为。
- **Mock（允许简化）**：所有 **外部 client**——HyperRAG、Actio（及经 Actio 的视频/视觉/OCR/ASR 等）、不依赖真实索引与 GPU 的 inspect 链路；answer_review 若依赖额外 LLM 可先 **固定通过或规则 stub**。
- **仍不做**：git submodule、**子智能体 delegate**；viknow2 业务代码不迁入跳板仓。

目标不是「再做一个 demo 三工具助手」，而是 **viknow2 换壳到 viknow-platform access + agentcore**，外部依赖用 mock 顶住，便于以后只换 client 实现。

## viknow2 基准（`config/.../profiles/viknow-v2.yaml`）

| 项 | viknow2 |
|----|---------|
| id | `viknow-v2` |
| label | 知识助手 |
| enabled_tools | `knowledge_discover`, `uploaded_image_search`, `query_refine`, `answer_plan`, `knowledge_search`, `video_inspect_2_0`, `visual_ground`, `document_inspect`, `list_video_segments`, `list_doc_segments` |
| enabled_skills | `knowledge-qa-v2`, `visual-ground` |
| 附加 | `system_prompt`, `answer_review`, `runtime_budget`, `user_prompt_template` |

工具实现入口：`viknow.core.tools.registry.build_viknow_tool_specs` + `agent_adapters/deepagents_tools.build_deepagents_tools(profile)`。

## viknow-platform 现状（`viknow-v3`，parity 进行中）

| 项 | 现状 |
|----|------|
| Profile | `knowledge_assistant/profile/viknow-v2.yaml` + `load_viknow_v2_profile()` |
| Registry | `viknow-v3` 登记 **10** 个 tool id，与 yaml 一致 |
| Tools | 10 个 StructuredTool，HyperRAG/Actio **mock** |
| Skills | 从 viknow2 同步 **knowledge-qa-v2** + **visual-ground** 全文 |
| Runtime | DeepAgents（可用时）/ ReAct 回退；LLM 见 `llm.py`（无 key 时仍用 script model） |
| 待补 | answer_review 运行时 gate、ToolSpec JSON 与 viknow2 字节级一致、真实 LLM 236 默认 |

## 「一比一」分层定义

```text
┌─────────────────────────────────────────────────────────┐
│ Access API（runs / events / library_ids）— 已按 viknow-platform 文档 │
├─────────────────────────────────────────────────────────┤
│ Profile 层：与 viknow-v2.yaml 同字段语义（可仍用 id viknow-v3 或别名） │
├─────────────────────────────────────────────────────────┤
│ Runtime：DeepAgents 图，由 agentcore 常驻 agent loop 直接运行（无 Agno；禁止长期依赖 ReAct 占位） │
├─────────────────────────────────────────────────────────┤
│ Skills：从 viknow2 拷贝/同步 knowledge-qa-v2 + visual-ground 全文 │
├─────────────────────────────────────────────────────────┤
│ Tools：10 个 tool 的 schema + handler 签名对齐 viknow2 ToolSpec │
├─────────────────────────────────────────────────────────┤
│ Clients（Mock）：HyperragClient / Actio* → 固定或可配置的 JSON fixture │
└─────────────────────────────────────────────────────────┘
```

**算对齐**：LLM 按 skill 调 `read_file` → 按 viknow2 顺序调 tool；Playground 收到与 viknow2 同结构的 tool_call / message 事件；工具返回字段与 viknow2 单测/样例一致（可来自 mock）。

**不算对齐**：仅 3 个工具、脚本模型硬编码、最终答案粘贴 raw mock JSON、缺少 `answer_plan` / inspect 类工具。

## Mock 边界（建议）

| 外部依赖 | viknow-platform 做法 |
|----------|----------------|
| HyperRAG | 扩展 `application/hyperrag/client.py` 或 `knowledge_assistant/mock_backend.py`：discover / search / refine / list_segments 返回与 viknow2 handler 同 shape 的 fixture |
| Actio | 新增 `application/actio/client.py`（Mock）：`video_inspect_2_0`, `visual_ground`, `document_inspect`, `uploaded_image_search` 返回带 `claims` / `candidates` 的占位结构 |
| answer_review | 配置保留；实现可为「score=80 恒通过」或跳过重试 |
| 真实 LLM | Playground 可继续 `server-default` / 私网模型；**与 mock client 正交** |

## 建议实现顺序（viknow-platform 分支）

1. **Profile 源**：YAML 或等价 Python 加载 viknow-v2 字段；Registry/API 暴露 `enabled_tools` / `enabled_skills`（与 viknow2 文档对齐）。
2. **Skills 目录**：`config/skills/` 与 viknow2 同路径约定；DeepAgents `skills=[...]` + system_prompt 与 yaml 一致。
3. **Tool 注册表**：按 viknow2 `ToolSpec` 复制 schema（可从 viknow2 测试/fixture 抽 JSON Schema）；handler 内只调 mock client。
4. **去掉 MVP 脚本模型**：默认走真实 ChatModel；仅测试环境可保留 stub。
5. **验收**：移植 viknow2 `test_deepagents_tools` / profile 解析类用例到 viknow3；Playground 手测多模态 tool 名出现在 SSE。

## API id 说明

- 对外可继续 **`profile_id=viknow-v3`**（避免破坏 Playground），**内容** 与 `viknow-v2` 等价；或在 Registry 增加 `viknow-v2` 别名指向同一 runtime。
- 与 viknow2 **二进制同 id** 非必须，**行为与配置同构** 必须。

---

维护：Cloud Agent / 236 `castmeta-research/viknow-platform` 分支 `agent/viknow-v3-knowledge-mvp`（及后续 parity 分支）。
