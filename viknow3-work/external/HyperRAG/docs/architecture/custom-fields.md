# HyperRAG 自定义字段验证分析

## 验证结果：✅ **完全可行**

通过深入分析 HyperRAG 源码，**可以在 chunking_func 的返回结果中添加自定义字段，并在后续处理中使用。**

## 完整的数据流分析

### 1. ChunkingFunc 阶段（数据产生）

**位置**: `hyperrag.py:1271-1278`
```python
chunks: dict[str, Any] = {
    compute_mdhash_id(dp["content"], prefix="chunk-"): {
        **dp,  # 🔑 关键：完整展开 chunking_func 返回的所有字段
        "full_doc_id": doc_id,
        "file_path": file_path,
        "llm_cache_list": [],
    }
    for dp in self.chunking_func(...)  # dp 包含所有自定义字段
}
```

**关键发现**：使用 `**dp` 语法完整展开 chunking_func 返回的字典，**所有自定义字段都会被保留**。

### 2. 存储阶段（数据持久化）

**KV存储** (`json_kv_impl.py:172`)：
```python
self._data.update(data)  # 直接更新，保留所有字段
```

**向量存储**：同样通过 `upsert(chunks)` 保存所有字段。

**关键发现**：存储层使用 `dict[str, dict[str, Any]]` 类型，**完全支持任意自定义字段**。

### 3. 实体提取阶段（数据使用）

**位置**: `operate.py:1574-1576`
```python
content = chunk_dp["content"]
file_path = chunk_dp.get("file_path", "unknown_source")  # 🔑 使用 .get() 访问字段
# 可以访问任意自定义字段：
# keywords = chunk_dp.get("keywords", [])
# layout_info = chunk_dp.get("layout_info", {})
```

**关键发现**：使用 `.get()` 方法访问字段，**可以轻松获取任意自定义字段**。

### 4. 查询检索阶段（数据获取）

**位置**: `operate.py:2014-2022`
```python
for result in results:
    if "content" in result:
        chunk_with_metadata = {
            "content": result["content"],
            "created_at": result.get("created_at", None),
            "file_path": result.get("file_path", "unknown_source"),
            # 🔑 可以添加自定义字段：
            # "keywords": result.get("keywords", []),
            # "layout_info": result.get("layout_info", {}),
        }
```

**关键发现**：查询结果包含完整的 chunk 数据，**可以通过 `result.get()` 访问所有自定义字段**。

## 自定义字段的应用场景

### 1. 文档结构信息
```python
{
    "tokens": 256,
    "content": "chunk内容...",
    "chunk_order_index": 0,
    "layout_info": {
        "page_number": 1,
        "section": "introduction", 
        "paragraph_index": 2,
        "bbox": [100, 200, 400, 250]  # 边界框坐标
    }
}
```

### 2. 内容分析结果
```python
{
    "tokens": 256,
    "content": "chunk内容...",
    "chunk_order_index": 0,
    "keywords": ["机器学习", "深度学习", "神经网络"],
    "sentiment": "positive",
    "topic": "AI技术",
    "language": "zh"
}
```

### 3. 多模态元数据
```python
{
    "tokens": 256,
    "content": "图像描述：一张显示数据趋势的图表...",
    "chunk_order_index": 0,
    "modality": "image",
    "original_path": "/path/to/image.jpg",
    "image_features": [0.1, 0.2, ...],  # 图像特征向量
    "ocr_confidence": 0.95
}
```

## 实现策略

### 方案一：直接在查询结果中使用
修改 `_get_vector_context` 函数，包含更多自定义字段：

```python
chunk_with_metadata = {
    "content": result["content"],
    "created_at": result.get("created_at", None),
    "file_path": result.get("file_path", "unknown_source"),
    "keywords": result.get("keywords", []),
    "layout_info": result.get("layout_info", {}),
    "modality": result.get("modality", "text"),
    # ... 其他自定义字段
}
```

### 方案二：创建增强的查询接口
创建自定义查询函数，完整保留所有字段：

```python
async def enhanced_query(
    query: str,
    rag_instance: HyperRAG,
    include_metadata: bool = True
) -> dict:
    # 获取原始查询结果
    chunks = await _get_vector_context_enhanced(...)
    # 返回包含所有自定义字段的结果
```

### 方案三：后处理增强
在查询后通过 chunk_id 从存储中获取完整数据：

```python
async def enrich_chunks_with_metadata(chunks, text_chunks_storage):
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id")
        if chunk_id:
            full_data = await text_chunks_storage.get_by_id(chunk_id)
            chunk.update(full_data or {})  # 合并所有字段
    return chunks
```

## 推荐实现路径

1. **阶段一**：在多模态 ChunkingFunc 中添加自定义字段
2. **阶段二**：验证字段在存储中的保留情况  
3. **阶段三**：修改查询接口以使用自定义字段
4. **阶段四**：在应用层利用这些字段进行高级功能

## 结论

**你的想法完全正确！** HyperRAG 的架构天然支持自定义字段：

✅ **存储保留**：所有自定义字段都会被完整保存  
✅ **处理访问**：在各个处理阶段都可以访问自定义字段  
✅ **查询获取**：查询结果可以包含自定义字段  
✅ **灵活扩展**：支持任意类型的元数据和分析结果  

这为构建高级的多模态 RAG 系统提供了强大的基础！