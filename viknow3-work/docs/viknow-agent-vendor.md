# Agents

多个 profile 可以共用 DeepAgents 回合，也可以自带另一套回合。外壳留在 `src/viknow3`。

## 原则

- **`src/viknow3/agents/profiles/`**：每个助手一份 YAML。`runtime` 决定走哪套回合。
- **`src/viknow3/agents/skills/`**：技能正文只放一份。两份 profile 的 `enabled_skills` 都指向这里。
- **`src/viknow3/agents/runtimes/deepagents/`**：DeepAgents 的回合循环、tools、harness（tool gate、stop gate、答案复核、`qa_plan_middleware`）、bubblewrap 沙箱。
- **`src/viknow3/agents/runtimes/adk/`**：ADK 的回合和工具函数。不导入 DeepAgents 回合。
- **`src/viknow3/`**：会话和 run、SSE、本轮可搜的库、HyperRAG client、Actio client。平台配置（模型地址、密钥、Postgres / Redis / Neo4j / S3）在 `viknow3_ops`（schema `ops`），由 `src/viknow3/ops/config/` 读取。DeepAgents runtime 只接收这些，不读 `app.yaml`，也不加载 `hyperrag` 或 `actio` 包。对话上下文由各自的 runtime 写：DeepAgents 写 LangGraph checkpoint，ADK 写自己的 session。
- Evidence Oracle 的收集流程由 profile 字段 `oracle_collection: true` 打开，不再按 profile id 写死。
- 飞书、ViCoach、viknow2 HTTP API、JuiceFS、索引流水线、eval/learning，以及旧的 `qa_box` 黑盒，没有留在这棵树里。
- Expert Buddy 的回合预算在 Playground 里关闭。长期记忆、Langfuse、Redis 调度仍被回合模块引用，Playground 不启用它们。

## 目录

| 路径 | 放什么 |
|------|------|
| `src/viknow3/agents/profiles/` | 助手档案。`expert_buddy.yaml` 是 `expertbuddy-deepagents`，`expert_buddy_adk.yaml` 是 `expertbuddy-adk` |
| `src/viknow3/agents/skills/` | `knowledge-qa-v2` 和 `visual-ground`，只保留一份 |
| `src/viknow3/agents/runtimes/deepagents/runtime.yaml` | 共用回合参数：图步数、沙箱、workspace、采样默认值。不含模型地址和密钥 |
| `src/viknow3/agents/runtimes/deepagents/` | DeepAgents 回合、harness、tools、沙箱 |
| `src/viknow3/agents/runtimes/adk/` | ADK 回合和工具函数。不经过 DeepAgents |
| `src/viknow3/service/agent/deepagents_adapter/` | 外壳把一次 run 交给 DeepAgents，并把输出写成 events |
| `src/viknow3/service/agent/adk_adapter/` | 外壳把 `runtime: adk` 的 run 交给 ADK，并把输出写成同一组 events |

`src/viknow3/agents/layout.py` 扫描 `src/viknow3/agents/profiles/*.yaml`，注册到 Agent Registry。带 `knowledge_search` 或 `knowledge_discover` 的 profile，首轮必须选一个知识库。

## 同步

不要再把 viknow2 整包同步成 `src/viknow`。DeepAgents 回合改 `src/viknow3/agents/runtimes/deepagents`。ADK 回合改 `src/viknow3/agents/runtimes/adk`。档案改 `src/viknow3/agents/profiles`，技能正文改 `src/viknow3/agents/skills`。

## 接线目标

外壳 `run_turn` 按 profile 的 `runtime` 选择适配器。`expertbuddy-adk` 走 `runtimes/adk`，`expertbuddy-deepagents` 走 DeepAgents。同一个 DeepAgents profile 在进程里只建一份 runtime。

当前状态：Playground 只登记这两个 agent。DeepAgents 用 agent 库连接自己维护 checkpoint。ADK 用同一条 `VIKNOW_AGENT_DSN` 写自己的 session，不读 LangGraph 的 checkpoint 表。`expertbuddy-deepagents` 不启用回合预算。
