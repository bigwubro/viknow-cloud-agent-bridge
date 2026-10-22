# HyperRAG
使用 SingleLayerKGService 的多模态 RAG 知识搜索引擎

## 设计架构

在 SingleLayerKGService 的图谱构建与搜索能力基础上，扩展了多模态解析（HyperParser）、智能感知分块（HyperChunker）和多模态嵌入（HyperEmbedder）能力。

## 视频索引与版本切换

视频的每个语义片段对应一个 chunk，在 `kg_index` 中使用视频切片和 caption/OCR 标签文本进行混合编码，只写入 KG chunk 向量表。BM25、展示及图谱抽取仍使用 chunk 文本。文本、图片和图文查询统一检索 KG，不再维护视频专用向量库或 `video_embedding` task。

部署切换时先停止旧 worker，完成或取消旧索引 job，再重建视频索引。旧视频向量表不再读写，也不会自动删除。缺失有效切片、时间范围错误或混合编码失败会使 KG 索引失败，不降级为纯文本、首帧或整视频编码。

混合编码切片缓存按源文件路径、大小、修改时间及精确时间范围标识。旧 caption 切片没有完整来源/区间证明，首次索引会从原视频重新裁剪；后续只复用匹配的缓存，防止编辑时间范围后误用旧画面。

环境配置需要手工调整，不自动改写：

- 仅使用统一的 `embedding_model/provider/base_url/api_key/dim`，或对应的 `models.embedding` 配置。
- 移除旧 `enable_video_embedding`、`video_embedding_*`、视频专用存储后端和旧 task 并发配置；应用层的 `models.video_embedding` / `vision_embedding` 不再作为模型来源。
- 媒体传输参数移至 `embedding_params`（嵌套配置为 `models.embedding.params`），包括 `local_video_mode: video_base64`、`max_inline_image_bytes`、`max_inline_video_bytes` 和请求 `timeout`。
- 实体/关系抽取的 LLM 并发使用顶层 `ER_extraction_max_async`，默认 8；不再使用顶层 `max_async`。
- KG 向量化请求并发使用 `models.embedding.max_async`，默认 32，覆盖文本批次、实体/关系文本和视频混合编码。运行时对应 `embedding_max_async`；各编码阶段顺序执行，每个索引调用独立限流，不是全服务请求上限。
- 文本每请求的批量大小使用 `kg_vector_embedding_batch_size`，默认 16；视频仍为一片段一请求。删除无效的 `embedding_batch_num` 和旧 `embedding_func_max_async`，旧键会明确报错，不做兼容映射。
- ViKnow2 知识库迁移包升级为 2.0，只携带统一 KG 向量。旧包需从重建索引后的知识库重新导出，不支持直接导入。

## TODO

测试创建两个hyperrag实例，一个查询，一个建索引
测试多个实例，一个投递任务，RW节点读取任务并执行
有时候会出现任务失败，导致job进度不动了，但是redis又不方便查看
asset status 和 asset processing status 有重复设计嫌疑