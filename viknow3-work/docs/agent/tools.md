# 工具和模型

工具是 profile 允许模型调用的函数。模型是这一轮推理用的聊天端点。工具名单写在 agent 包里。模型端点写在 `viknow_ops`，不写在 profile，也不写在 `runtime.yaml`。

## 工具

`enabled_tools` 决定这个 agent 能看见哪些工具。外壳出站网关的 `tool_allowed` 用 Registry 里保存的这组名字做检查。知识库相关工具不自己创建引擎，它们调用注入的 `KnowledgeClient`。

`KnowledgeClient` 上 runtime 会用到的方法：

| 方法 | 用途 |
| --- | --- |
| `default_library_id` | 请求没指定库时的缺省库 |
| `list_libraries` / `discover_libraries` | 列出或按问题发现库。同步和异步各有一份 |
| `search` / `search_async` | `library_id`、`query`、`top_k`、`threshold` |
| `search_by_image` | 用图片检索 |
| `query_kg_async` | 知识图谱查询 |
| `list_asset_segments` | 一个资产的片段 |
| `get_chunk_by_id` | 按 evidence id 取 chunk |
| `get_asset_status` | 资产状态 |
| `evidence_for_chunk` | 把 chunk 收成证据 |
| `resolve_document_render_path` | 文档渲染路径 |

视频检查、语音转写、OCR、视觉定位、视频发现和抽帧走注入的 `ActioClient`。工具不自己构造这些客户端，也不加载 `actio` 包。没有注入 client 时，这些工具报 `actio client is required`。

知识助手的技能要求：匹配技能时，第一个工具调用是 `read_file`，读该技能的 `SKILL.md`。最终给用户的正文放在没有工具调用的消息里。回答检查开着，`max_retries` 是 0，`allow_low_confidence_output` 是 true。这是 `expertbuddy-deepagents`。`expertbuddy-adk` 不读这份技能，它自己的 `knowledge-qa` 只引用 `chunk_text`，工具函数在 `agents/adk_buddy/knowledge_tools.py`，调用的是同一个注入的知识库 client。

运维助手的工具是 Prometheus、vLLM 和 NPM 的查询与健康检查，不是知识库工具。证据采集器在检索之外还有一组 `evidence_*` 工具。各包的完整名单以 profile YAML 为准。

沙箱里的 `execute` 由 DeepAgents 工作区提供。`runtime.yaml` 把工作区设为 bubblewrap，默认无网络。这不是一条 HTTP 工具。

## 模型

外壳的 `build_agent_app_config` 从 `viknow_ops` 读 provider 和模型名，从 `runtime.yaml` 的 `model` 块读温度、重复惩罚、最大输入和超时，拼成 runtime 用的 `AppConfig`。这个函数不读 `app.yaml`。

`GET /api/v1/agent/models` 返回：

- `default_model`：`server-default`
- `models`：第一项是 `server-default`，标签里带解析后的真实模型名；后面是代码里的 `ALLOWED_MODELS`（当前为 `siliconflow:Qwen/Qwen3.6-35B-A3B`）

请求里的 `agent_model` 或 `model` 必须是已知模型，否则 400，`code` 为 `unknown_model`。不传时用 profile 的 `default_model`。Registry 里这份缺省目前是空。

检查和 `query_refine` 用的模型，由 `review_tool_configs` 指到同一个平台聊天模型。没有单独的 tools YAML。

`VIKNOW_USE_SCRIPT_MODEL=1` 只给测试。它关掉回答检查的 LLM 评审和 gate，并用脚本模型代替真实聊天模型。Playground 进程不设这个变量。

## 下一步

- [配置](../ops/configuration.md)
- [Runtime](runtime.md)
- [知识库](../knowledge/overview.md)
