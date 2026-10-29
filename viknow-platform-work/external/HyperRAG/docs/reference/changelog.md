# 变更日志

> 记录 HyperRAG 的关键变更与迁移提示。

## 2025-08-15

- Chunker 重构（HyperChunker）
  - 统一输出字段：仅保留 `content`, `tokens`, `chunk_order_index`, `file_path`, `doc_id`, `segment_ids`, `order`；移除 `text`, `token_count` 冗余字段。
  - 合并 `_normalize_hyperrag_chunks` 到 `_make_chunk`：
    - `_make_chunk(**chunk)` 直接接受 HyperRAG 默认切分产物（`content/tokens/chunk_order_index/...`）并标准化。
    - `_make_chunk(content=..., tokens=..., chunk_order_index=..., ...)` 支持我们自建 chunk 的统一构造。
  - 段落聚合策略升级：
    - 文本跨模态连续聚合：`text` 与 `video_clip` 默认并入“文本缓冲区”，遵循 `max_token_size`，并按需添加 `overlap`；中途出现 `image/table` 不打断文本缓冲。
    - 模态原子化：`image/table` 段各自生成独立 chunk；可配置将“小表格”并入文本。
    - 语义分段（可选）：开启 `respect_semantic_boundaries=True` 时，遇到 Markdown 标题（行首 `#`/`##` 等）或视频 `scene_break`，即使未达上限也会切段。
    - 可选文本标记：`insert_modal_markers_in_text=True` 时在文本缓冲插入 `[图像]`/`[表格]` 标记。
  - video_clip 默认视为文本并入缓冲（移除开关）。
  - 保持与 HyperRAG 兼容：`file_path` 使用 `doc_id`，顺序字段统一为 `chunk_order_index/order`。
  - 性能与健壮性：使用近似 token 切分策略；当 `chunking_by_token_size` 不可用时兜底为单块。

## 2025-08-14

- 强制外部传入 doc_id（asset_id）
  - `HyperRAG.add_asset(asset_path, asset_id)` 现在必须由调用方显式提供稳定的 `asset_id`（即 doc_id）。
  - 不再在内部生成随机 ID。未传或为空将抛出 `ValueError`。
  - 迁移建议：为同一源文件生成稳定 `doc_id`（例如基于文件内容哈希、或绝对路径+大小+mtime）。

- 已完成/进行中文档的幂等与跳过
  - 入队前会查询 HyperRAG `doc_status`：
    - 若状态为 `PROCESSED`：直接将 `AssetStatus` 置为 `COMPLETED`，跳过后续流程。
    - 若状态为 `PROCESSING`/`PENDING`：不重复入队，仅同步 `AssetStatus=INDEXING` 并返回。

- 将 `hyperrag.ainsert` 改为分段调用
  - 分段流程：
    1) 生成 `track_id = generate_track_id("insert")`
    2) `apipeline_enqueue_documents(input, ids=[doc_id], file_paths=[source_uri], track_id)`
    3) 立即调用 `await _insert_done()` 触发落盘（避免处理中断丢失 `full_docs`）
    4) `await apipeline_process_enqueue_documents()`
    5) 等待 `doc_status==PROCESSED`
  - 始终传入 `file_paths=[asset.source_uri]`，便于诊断与幂等。

- 处理进度百分比规范
  - 进入 `PARSING`：0.1
  - 进入 `CHUNKING`：0.2
  - 进入 `INDEXING`：0.3
  - HyperRAG 完成索引后：
    - 文档：直接 1.0 完成
    - 视频：先 0.9，完成视频片段向量化后 1.0 完成

- 退出安全与文件锁释放
  - 在 `shutdown()` 中于 `finalize_storages()` 之前执行 `await hyperrag._insert_done()` 与 `await status_storage.index_done_callback()`，尽可能落盘。
  - 在 `start()` 中注册 `SIGINT/SIGTERM` 信号处理，触发 `stop()`，确保优雅关闭。
  - 保留 `atexit` 兜底；新增 `__del__` 兜底释放工作区文件锁。

- 行为影响与兼容性
  - 需要调用方提供稳定 `asset_id`（doc_id）。
  - 目录重跑时，已完成与在队中文档会被跳过，不再重复索引或误置为失败。
  - 中途中断后重启，已入队的文档由于提前落盘，能继续处理而不丢 `full_docs`。


