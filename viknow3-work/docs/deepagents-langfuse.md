# DeepAgents 问答 Langfuse（独立 project）

Playground **外壳**不上报 Langfuse；只有 **DeepAgents runtime** 内的 LangChain callback + 工具内 generation 写入 trace。

## Project

- Langfuse UI：`http://test.ai.qingxiang.tech:3000`（236 内网 `http://127.0.0.1:3000`）
- Project 名：**`viknow3-deepagents`**（与 HyperRAG / 旧 `VIKNOW_LANGFUSE_*` project 分离）
- Trace 名：**`viknow3-deepagents`**
- Environment 标签：**`viknow3`**

## 236 进程环境变量

仅 DeepAgents Playground API 需要（写在 `/home/cursor-agent/.viknow3/deepagents-langfuse.env`，**不要提交 git**）：

```bash
VIKNOW_DEEPAGENTS_LANGFUSE_HOST=http://127.0.0.1:3000
VIKNOW_DEEPAGENTS_LANGFUSE_PUBLIC_KEY=pk-lf-...
VIKNOW_DEEPAGENTS_LANGFUSE_SECRET_KEY=sk-lf-...
```

`scripts/deploy-playground-ui.sh` 已通过 `VIKNOW3_ENV_FILE` 追加加载该文件。  
**不要**把上述 key 写进 `VIKNOW_LANGFUSE_*` 或 HyperRAG 的 `LANGFUSE_*`，避免串 project。

## 代码入口

- `build_langfuse_callbacks()`：`agent_adapters/langfuse.py` — 用 `VIKNOW_DEEPAGENTS_*` 注册独立 `Langfuse` client + `CallbackHandler(public_key=...)`
- `_trace_metadata()`：`deepagents.py` — `viknow_run_id`、`session_id`、`user_id`、`library_ids` 等写入 trace metadata

## 冒烟

1. `bash scripts/deploy-playground-ui.sh restart`
2. Playground 提一问（`expertbuddy-deepagents`，绑定 lib）
3. 用 **同一 project 的 API key** 调 `GET /api/public/traces`，按 `metadata.viknow_run_id` 或 trace 名 `viknow3-deepagents` 查找

未配置 `VIKNOW_DEEPAGENTS_*` 时 callback 列表为空，行为与关闭 tracing 相同。
