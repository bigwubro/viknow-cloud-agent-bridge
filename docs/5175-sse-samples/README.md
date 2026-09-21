# 5175 Agent SSE 实测样例

存放 **完整、未删节** 的 `GET /api/v1/agent/runs/{run_id}/events` 拉流原文（SSE `event:` / `data:` 格式），供飞书等文档通过外链引用，避免单文档体积过大。

## `e42466f1-knowledge-media-citations.sse`

| 项 | 值 |
|----|-----|
| 环境 | `http://36.103.198.236:5175` |
| run_id | `e42466f1-e3a2-44b3-8d91-c3815ba4ff57` |
| 场景 | `library_ids=["default"]`，知识库 PDF + `document_inspect`，答案带 `[1]` 媒体引用 |
| 事件数 | 28 |
| SHA256 | `dd6ddaff3c2b09cb8e8a499490862f877173104cd5f471ae668db312394ec25b` |

复现拉流（run 已结束后仍可读历史事件）：

```http
GET /api/v1/agent/runs/e42466f1-e3a2-44b3-8d91-c3815ba4ff57/events?after_sequence=0
```

媒体引用见文件中 `event: message_completed`（`sequence`: 27）的 `payload.citations`。

## `knowledge_search-wiki-doc-test-demo.json`

| 项 | 值 |
|----|-----|
| 接口 | `POST /api/v1/knowledge/search` |
| 请求 | `{"library_id":"wiki-doc-test","query":"demo","top_k":1}` |
| 说明 | **完整**响应（含 `data` 与根级镜像字段、`parts[text+file]`、`metadata.asset_id`） |

与飞书文档 **§5.1.4** 一致；字段说明见同目录 `KNOWLEDGE_SEARCH_RESPONSE.md`。
