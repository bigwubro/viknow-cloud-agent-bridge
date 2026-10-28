# 知识库

知识库是外壳持有的 HyperRAG 引擎。Playground 把**库与文件**交给 **viknow-storage**，把**索引、检索、在线状态**交给 **ViKnow API**（`viknow3.server`，默认 `:5175`）。agent 的一轮不启动第二份引擎，只调用装配时注入的 `KnowledgeClient`。

你可以用知识库做这些事：

- 在 storage 建库、上传资产；在 ViKnow 提交索引任务、查询索引进度。
- 用检索接口查询一个库。
- 让启用了知识库工具的 agent 在本轮被授予的那一个库里发现和检索。

## 引擎和 client

进程启动时 `server` 的 lifespan 调用 `init_hyperrag`。关闭时调用 `shutdown_background_runtime`。

适配器装配 DeepAgents 时调用 `viknow3.service.hyperrag.runtime.get_engine_adapter()`。返回值满足 `viknow3.agents.runtimes.deepagents.core.knowledge.client.KnowledgeClient`。工具使用公开方法，例如 `search`、`discover_libraries`、`evidence_for_chunk`。runtime 里没有第二份 HyperRAG 适配器。

引擎自己的连接使用 `VIKNOW_HYPERRAG_DSN`。外壳另外还有一套面向 HTTP 的 HyperRAG 配置加载，在 `service/hyperrag/config.py`，和 agent runtime 的配置不是同一个函数。

检索失败时，agent 工具看到的是 client 的返回。外壳不会把引擎内部对象写进 `run_events`。

## Actio client

Actio 和 HyperRAG 用同一种接法。索引和多模态工具调用外壳注入的 client，不加载 `actio` 包。

`viknow3.service.actio` 是唯一加载 `actio` 包的位置。`viknow3.agents.runtimes.deepagents.core.actio.client.ActioClient` 是 runtime 看到的协议。装配 DeepAgents 时，`get_actio_client()` 把实现传进 `build_agent_runtime`，和 `KnowledgeClient` 放在一起。

视频解析在 HyperRAG 里。HyperRAG 不加载 `actio`，也不引用 `viknow3`。`init_hyperrag` 调用 `install_actio_port()`，把解析器、caption 观察和 `normalize_global_context` 装进 `hyperrag.actio_port`。任务处理器只调用这个端口。端口还没装上时，视频解析失败，错误是 `actio is required for video_parse`。

`import hyperrag` 只出现在 `src/viknow3/service/hyperrag/`。

## HTTP（ViKnow API）

路径前缀 `/api/v1/knowledge`，需要 app key（`X-App-Key` 或 Bearer）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/index-jobs` | 提交索引（body：`library_id`、`asset_id`；从 storage 拉字节） |
| GET | `/index-jobs/{job_id}` | 任务状态 |
| GET | `/index-jobs/{job_id}/tasks` | 子任务 |
| GET | `/libraries/{library_id}/assets/index-status-batch` | 批量索引进度 |
| GET | `/libraries/{library_id}/assets/{asset_id}/index-status` | 单资产进度 |
| GET | `/libraries/{library_id}/assets/{asset_id}/index-jobs/latest` | 最近索引任务 |
| POST | `/search` | 检索 |
| DELETE | `/indexes` | 删除索引 |
| GET | `/api/v1/online/libraries/{library_id}/assets/{asset_id}/index` | 在线索引元数据 |

库列表、上传、预览、下载不在 ViKnow API 上；Playground 直连 **viknow-storage**（`VIKNOW_STORAGE_URL` + Bearer token）。

索引任务的阶段和进度字段见 [索引任务状态](../guide/knowledge-index-job-status.md)。部署和子模块见 [HyperRAG Runbook](../guide/hyperrag-runbook.md)。

## 下一步

- [Agent Runtime](../agent/runtime.md)：client 在哪一次装配里注入。
- [工具和模型](../agent/tools.md)：哪些方法会被工具调用。
