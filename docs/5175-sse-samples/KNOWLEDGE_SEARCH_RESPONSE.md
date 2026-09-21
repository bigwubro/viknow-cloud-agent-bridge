| 项 | 内容 |
|-|-|
| **作用** | 对指定知识库做 hybrid 检索，返回 **Evidence** 片段；**不生成最终答案** |
| **方法/路径** | `POST /api/v1/knowledge/search` |
| **与 Agent 关系** | 与 SSE 中 `tool_call_completed.name=knowledge_search` 的 `payload.result` **同一 JSON 结构**（字符串化后嵌入 SSE） |
| **前置条件** | 对应资产 index-jobs 状态为 `done` |
| **实测状态码** | `200` |

**请求 body**

| 字段 | 必填 | 类型 | 说明 |
|-|-|-|-|
| library_id | 是 | string | 知识库 ID |
| query | 是 | string | 检索问题 |
| top_k | 否 | int | 返回条数上限，默认 10 |

### 5.1.1 三个 ID 怎么区分（预览文件看哪个）

| 字段 | 示例 | 含义 | Trail 用途 |
|-|-|-|-|
| `library_id` | `wiki-doc-test` | 知识库 | 拼 `/media/libraries/{library_id}/...` |
| `evidence[].id` / `metadata.chunk_id` | `chunk-c50510db...` | **检索片段 / evidence id** | `document_inspect` 的 `evidence_id`；**不是**预览文件 id |
| `metadata.asset_id` / `metadata.full_doc_id` | `asset-search-demo` 或 UUID | **知识库资产（源文件）id** | **预览/下载文件用这个** |
| `parts[kind=file].metadata.asset_id` | 同上 | 与 `metadata.asset_id` 一致 | 若只看 `parts[0]` 会误以为没有文件 id，**必须看 `parts` 全文或 `metadata`** |

> 5175 **没有**名为 `preview_file_id` 的字段；应用端预览统一使用 **`asset_id` + `library_id`**（见 **§6** 或下文 URL 表）。

### 5.1.2 从检索结果到预览 URL

设 `library_id=L`，`asset_id=A`（来自 `evidence[].metadata.asset_id`），页码 `page` 来自 `evidence[].metadata.locations[].page`（**从 0 起**）：

| 场景 | 方法/路径 |
|-|-|
| 整份文档（PDF/渲染源） | `GET /media/libraries/L/assets/A/document` |
| 文档页图（PDF 等） | `GET /media/libraries/L/assets/A/document/pages/{page}.png` |
| 视频源 | `GET /media/libraries/L/assets/A/video` |
| 资产内图片 | `GET /media/libraries/L/assets/A/images/{image_name}` |

**实测（`wiki-doc-test`）**：`L=wiki-doc-test`，`A=asset-search-demo` → 上述 `document` 与 `pages/0.png` 均为 `200`。

生产环境若对象在 S3 私有桶，应用 BFF 可对 `knowledge/libraries/{L}/{A}.{ext}` 做 **presigned GET**（见 **§1.6**）；浏览器仍可用 ViKnow 的 `/media/...` 只读路由（test 5175 直连）。

### 5.1.3 响应 envelope（顶层字段）

| 字段 | 说明 |
|-|-|
| `tool` | 固定 `knowledge_search` |
| `status` / `code` | `ok` 表示检索成功（**无命中**时 `evidence` 为空数组，仍为 `ok`） |
| `data` | **推荐解析入口**：`data.library_id`、`data.query`、`data.evidence[]`、`data.metadata` |
| 根级 `library_id` / `query` / `evidence` / `metadata` | 与 `data.*` **内容重复**（tool 序列化时的镜像字段）；实现上 **任选一处解析即可**，不要混用两套计数 |

### 5.1.4 响应 body（实测有命中，`wiki-doc-test`，**完整 JSON，无省略**）

请求：`{"library_id":"wiki-doc-test","query":"demo","top_k":1}`

```json
{
  "tool": "knowledge_search",
  "status": "ok",
  "code": "ok",
  "data": {
    "library_id": "wiki-doc-test",
    "query": "demo",
    "evidence": [
      {
        "id": "chunk-c50510dba655393a3cff8d7bbfa81272",
        "source": "knowledge_search",
        "title": "search demo",
        "score": 0.5286077857017517,
        "parts": [
          {
            "kind": "text",
            "text": "### context\n\n\"第 1 个片段位于文档摘要之后，作为 ViKnow 搜索探针的具体示例。它直观展示了 DDR4 与 DDR5 在电压和带宽上的关键差异，旨在帮助读者快速理解不同代际内存的性能与能效特征。\"\n\n### chunk text\n\n\"ViKnow search probe: DDR4 uses 1.2V, DDR5 uses 1.1V and has higher bandwidth.\"",
            "uri": null,
            "mime_type": null,
            "time_range": null,
            "metadata": {
              "chunk_id": "chunk-c50510dba655393a3cff8d7bbfa81272"
            }
          },
          {
            "kind": "file",
            "text": null,
            "uri": "/data/viknow/knowledge/libraries/wiki-doc-test/asset-search-demo.txt",
            "mime_type": null,
            "time_range": null,
            "metadata": {
              "asset_id": "asset-search-demo"
            }
          }
        ],
        "metadata": {
          "edited": false,
          "locations": [
            {
              "bbox": [
                53,
                54,
                518,
                68
              ],
              "page": 0,
              "text": "ViKnow search probe: DDR4 uses 1.2V, DDR5 uses 1.1V and has higher bandwidth.",
              "page_size": [
                595,
                841
              ],
              "segment_id": "asset-search-demo_seg_0000",
              "total_pages": 1
            }
          ],
          "chunk_type": "text",
          "segment_ids": [
            "asset-search-demo_seg_0000"
          ],
          "file_path": "/data/viknow/knowledge/libraries/wiki-doc-test/asset-search-demo.txt",
          "order": 0,
          "asset_id": "asset-search-demo",
          "title": "search demo",
          "rerank_score": 0.5286077857017517,
          "rerank_model": "Qwen/Qwen3-VL-Reranker-2B",
          "library_id": "wiki-doc-test",
          "full_doc_id": "asset-search-demo",
          "chunk_id": "chunk-c50510dba655393a3cff8d7bbfa81272"
        }
      }
    ],
    "metadata": {
      "retrieval_mode": "hybrid",
      "result_count": 1
    }
  },
  "library_id": "wiki-doc-test",
  "query": "demo",
  "evidence": [
    {
      "id": "chunk-c50510dba655393a3cff8d7bbfa81272",
      "source": "knowledge_search",
      "title": "search demo",
      "score": 0.5286077857017517,
      "parts": [
        {
          "kind": "text",
          "text": "### context\n\n\"第 1 个片段位于文档摘要之后，作为 ViKnow 搜索探针的具体示例。它直观展示了 DDR4 与 DDR5 在电压和带宽上的关键差异，旨在帮助读者快速理解不同代际内存的性能与能效特征。\"\n\n### chunk text\n\n\"ViKnow search probe: DDR4 uses 1.2V, DDR5 uses 1.1V and has higher bandwidth.\"",
          "uri": null,
          "mime_type": null,
          "time_range": null,
          "metadata": {
            "chunk_id": "chunk-c50510dba655393a3cff8d7bbfa81272"
          }
        },
        {
          "kind": "file",
          "text": null,
          "uri": "/data/viknow/knowledge/libraries/wiki-doc-test/asset-search-demo.txt",
          "mime_type": null,
          "time_range": null,
          "metadata": {
            "asset_id": "asset-search-demo"
          }
        }
      ],
      "metadata": {
        "edited": false,
        "locations": [
          {
            "bbox": [
              53,
              54,
              518,
              68
            ],
            "page": 0,
            "text": "ViKnow search probe: DDR4 uses 1.2V, DDR5 uses 1.1V and has higher bandwidth.",
            "page_size": [
              595,
              841
            ],
            "segment_id": "asset-search-demo_seg_0000",
            "total_pages": 1
          }
        ],
        "chunk_type": "text",
        "segment_ids": [
          "asset-search-demo_seg_0000"
        ],
        "file_path": "/data/viknow/knowledge/libraries/wiki-doc-test/asset-search-demo.txt",
        "order": 0,
        "asset_id": "asset-search-demo",
        "title": "search demo",
        "rerank_score": 0.5286077857017517,
        "rerank_model": "Qwen/Qwen3-VL-Reranker-2B",
        "library_id": "wiki-doc-test",
        "full_doc_id": "asset-search-demo",
        "chunk_id": "chunk-c50510dba655393a3cff8d7bbfa81272"
      }
    }
  ],
  "metadata": {
    "retrieval_mode": "hybrid",
    "result_count": 1
  }
}
```

**`evidence[]` 单条结构说明**

| 路径 | 说明 |
|-|-|
| `id` | evidence / chunk id（`chunk-...`） |
| `source` | 固定 `knowledge_search` |
| `title` | 资产标题 |
| `score` | 相关度 |
| `parts[]` | 多模态片段；通常 **至少 2 项**：`kind=text`（可读摘要）+ `kind=file`（源文件定位，`uri` 为容器内路径，`metadata.asset_id` 为资产 id） |
| `parts[].kind` | `text` / `file` / `image` / `video`（检索结果常见 text+file） |
| `parts[].uri` | 容器内文件路径，如 `/data/viknow/knowledge/libraries/{library_id}/...`；**不要**直接给客户端，应转为 `/media/...` 或 presign |
| `metadata.asset_id` | **预览/下载用的资产 id** |
| `metadata.full_doc_id` | 多数情况下与 `asset_id` 相同 |
| `metadata.file_path` | 与 `parts[kind=file].uri` 一致的源路径 |
| `metadata.locations[]` | 版面框选：`page`（0 起）、`bbox`、`segment_id`、`total_pages` 等 |
| `metadata.chunk_type` | 如 `text` |
| `metadata.library_id` | 冗余携带库 id |
| `metadata.rerank_score` / `rerank_model` | 重排信息 |

### 5.1.5 响应 body（实测无命中，`default`）

```json
{
  "tool": "knowledge_search",
  "status": "ok",
  "code": "ok",
  "data": {
    "library_id": "default",
    "query": "DDR4和DDR5的区别",
    "evidence": [],
    "metadata": {
      "retrieval_mode": "hybrid",
      "result_count": 0
    }
  },
  "library_id": "default",
  "query": "DDR4和DDR5的区别",
  "evidence": [],
  "metadata": {
    "retrieval_mode": "hybrid",
    "result_count": 0
  }
}
```

### 5.1.6 Agent 问答里的媒体引用（与检索的关系）

| 阶段 | 事件 / 字段 | 说明 |
|-|-|-|
| 检索 | `tool_call_completed` → `knowledge_search` | 本节 JSON；含 `asset_id`，但 **未** 直接给出可给浏览器的 `/media/...` 短链 |
| 答案落盘 | `message_completed.payload.citations` | **推荐 Trail 展示引用时解析这里**；含 `source.uri`、`render.url`、`anchors[].page_image_url`（已为 `/media/libraries/...` 路径） |
| 深挖 | `document_inspect` | 入参需 `library_id`、`asset_id`、`evidence_id`（= `evidence[].id`） |

带 PDF 页图引用的 **完整 SSE 外链**（28 事件）：见 **§3.4 附录**（GitHub Raw `e42466f1-knowledge-media-citations.sse`）。

> 面向用户的完整问答请用 **3.2 开聊 + 3.4 SSE**；`POST /api/v1/knowledge/search` 用于联调、排查命中与确认 `asset_id`。

