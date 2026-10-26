# Agents

多个 profile 可以共用 DeepAgents 回合，也可以自带另一套回合。外壳留在 `src/viknow3`。

## 原则

- **`src/agents/runtime/`**：DeepAgents 的回合循环、tools、harness（tool gate、stop gate、答案复核、`qa_plan_middleware`）、bubblewrap 沙箱。
- **`src/agents/adk_buddy/`**：ADK 的 profile、技能和回合。不导入 `agents.runtime`，也不提供 inspect 工具链。
- **`src/agents/<agent>/`**：DeepAgents agent 自己的 profile 和 skills。
- **`src/viknow3/`**：会话和 run、SSE、本轮可搜的库、HyperRAG client、Actio client。平台配置（模型地址、密钥、Postgres / Redis / Neo4j / S3）在 `viknow_ops`，由 `src/viknow3/ops/config/` 读取。DeepAgents runtime 只接收这些，不读 `app.yaml`，也不加载 `hyperrag` 或 `actio` 包。对话上下文由各自的 runtime 写：DeepAgents 写 LangGraph checkpoint，ADK 写自己的 session。
- Evidence Oracle 的收集流程由 profile 字段 `oracle_collection: true` 打开，不再按 profile id 写死。
- 飞书、ViCoach、viknow2 HTTP API、JuiceFS、索引流水线、eval/learning，以及旧的 `qa_box` 黑盒，没有留在这棵树里。
- Expert Buddy 的回合预算在 Playground 里关闭。长期记忆、Langfuse、Redis 调度仍被回合模块引用，Playground 不启用它们。

## 目录

| 路径 | 放什么 |
|------|------|
| `src/agents/runtime/runtime.yaml` | 共用回合参数：图步数、沙箱、workspace、采样默认值。不含模型地址和密钥 |
| `src/agents/runtime/` | 共享回合、harness、tools、沙箱 |
| `src/agents/expert_buddy/profile/`、`skills/` | 知识助手。Playground 对外 id 仍是 `deepagents` |
| `src/agents/adk_buddy/` | 知识助手（ADK）。对外 id 是 `adk`，回合不经过 DeepAgents |
| `src/agents/video_buddy/` | 进阶视频 profile 和 `advanced-video-qa` |
| `src/agents/evidence_oracle/` | Evidence Oracle profile 和 `evidence-oracle-*` skills |
| `src/agents/doc_eval/` | MMLongBench 的 eval / extract 两个 profile |
| `src/agents/ops/` | 运维 profile 和监控工具配置 |
| `src/viknow3/application/agent/deepagents_adapter/` | 外壳把一次 run 交给 DeepAgents，并把输出写成 events |
| `src/viknow3/application/agent/adk_adapter/` | 外壳把 `runtime: adk` 的 run 交给 ADK，并把输出写成同一组 events |

`src/agents/layout.py` 扫描带 `profile/*.yaml` 的目录，注册到 Agent Registry。带 `knowledge_search` 或 `knowledge_discover` 的 profile，首轮必须选一个知识库。

## 同步

不要再把 viknow2 整包同步成 `src/viknow`。共享回合改 `src/agents/runtime`。某个 agent 的 profile 或 skill 改它自己的目录。

## 接线目标

外壳 `run_turn` 按 profile 的 `runtime` 选择适配器。`adk` 走 `adk_buddy`，其他已登记 profile 走 DeepAgents。同一个 DeepAgents profile 在进程里只建一份 runtime。

当前状态：Playground 的 `deepagents` 走 expert_buddy，`adk` 走 adk_buddy。Registry 同时列出 video_buddy、evidence_oracle、doc_eval 的两个 profile，以及 ops。DeepAgents 用 agent 库连接自己维护 checkpoint。ADK 用同一条 `VIKNOW_AGENT_DSN` 写自己的 session，不读 LangGraph 的 checkpoint 表。expert_buddy 不启用回合预算。
