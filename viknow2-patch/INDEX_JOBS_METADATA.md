# index-jobs `metadata` 不稳定（5175）

## 现象

`GET /api/v1/knowledge/index-jobs/{job_id}` 在 Redis job `status=completed`（API 映射为 `done`）时，`metadata` 有时为 `null`，有时有 `doc_type` / `page_count`。

## 根因（5175 实测）

1. **API 只读 `get_asset_status().metadata`**（`viknow/api/app.py` → `_resolve_index_job_metadata`），而 HyperRAG 在 `COMPLETED` 时通过 `aget_asset_metadata()` **动态合并** metadata，并不保证 PG `metadata` JSONB 有值。
2. **文档类**：`aget_asset_metadata` 依赖 MinerU 缓存 `mineru_cache/{asset_id}/vlm/index.json` 里的 `page_info.total_pages`。缓存缺失或 `doc_id` 不一致时返回 `None`，即使资产已 `completed`（实测 `camera` 库多份 PDF 如此）。
3. **视频类**：依赖 `video_cache/.../sop-full.json` 或 `index.json` 的 `duration_sec` 等；缺失则 `metadata` 为空。
4. **历史 job**：部分 Redis `workspace` 存的是旧文件名而非 `library_id`，`get_asset_status(workspace, asset_id)` 直接 **404（None）**；但 Redis 里仍有正确 `asset_path`（`/data/viknow/knowledge/uploads/{library_id}/...`）。

## 修复（`viknow2-patch/app.py`）

- `done` 时按 **`workspace` + 从 Redis `asset_path` 解析的 library_id** 依次查资产状态。
- 若 HyperRAG 未返回 metadata，则根据 **`parse_mode` + `file_path`** 回退：文档 PDF 用 `pypdf` 统计 `page_count`，至少返回 `doc_type`。

上游还应考虑在 HyperRAG 索引完成时 **持久化 metadata 到 `HYPERRAG_ASSET_STATUS.metadata`**，避免每次读缓存。

## 合入 viknow2

将 `viknow2-patch/app.py` 中相关函数合入 `viknow/api/app.py`，并运行 `tests/api/test_knowledge_index_job_routes.py`。
