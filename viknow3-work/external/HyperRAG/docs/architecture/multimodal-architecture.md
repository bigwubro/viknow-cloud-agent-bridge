# HyperRAG 多模态处理分析

## 并发与实例管理补充

- 每个 workspace 一个 HyperRAG 实例，由进程内注册表管理；跨进程通过 `portalocker` 文件锁互斥，锁文件内写入 `pid/ts`，便于排障。
- 专用事件循环线程：索引任务经内部 `asyncio.Queue` 分发；查询直接调用 `HyperRAG.aquery`。
- 解析阶段使用 `asyncio.to_thread` 放入线程池，避免阻塞事件循环；解析心跳周期性写入 `PARSING` 状态更新时间。
- 取消与失败：`CancelledError` → `CANCELLED`，其他异常 → `FAILED`（带堆栈日志）。
- 初始化顺序：`initialize_share_data()` → `initialize_pipeline_status()` → `await hyperrag.initialize_storages()`；确保 HyperRAG 的共享存储与管道状态就绪。

```mermaid
flowchart TB
  subgraph 单进程单活实例
    R[Registry.get_instance] --> H[HyperRAG]
    H -->|start() 事件循环线程| Loop
    H -->|add_asset| Q[asyncio.Queue]
    Loop --> W1 & W2 & W3
    W1 -->|to_thread parse| Parser
    Parser --> W1 --> L[HyperRAG]
    L --> W1 --> S[(AssetStatusStorage)]
    H -->|query| L
  end
  subgraph 跨进程互斥
    F[.hyperrag.lock] --- H
  end
```

## HyperRAG 数据处理流程

```
输入数据 → ChunkingFunc → 实体提取 → 知识图谱构建 → 向量化存储
```

### 1. ChunkingFunc 的关键地位

从源码分析可以看到，`chunking_func` 是 HyperRAG 处理流程的第一步，具有以下特点：

**函数签名：**
```python
chunking_func: Callable[
    [
        Tokenizer,      # 分词器
        str,           # 输入内容 - 这里是关键！
        Optional[str], # 分割字符
        bool,          # 仅按字符分割
        int,           # chunk_token_size
        int,           # chunk_overlap_token_size
    ],
    List[Dict[str, Any]], # 返回chunk列表
]
```

**返回格式：**
```python
[
    {
        "tokens": int,              # chunk的token数量
        "content": str,             # chunk的文本内容
        "chunk_order_index": int,   # chunk的顺序索引
    }
]
```

### 2. 在处理流程中的位置

在 `apipeline_process_enqueue_documents` 方法的第1271行：

```python
for dp in self.chunking_func(
    self.tokenizer,
    content,  # 这里是原始输入内容
    split_by_character,
    split_by_character_only,
    self.chunk_overlap_token_size,
    self.chunk_token_size,
):
    # 处理每个chunk
```

### 3. 多模态处理的可行性分析

#### ✅ **输入层面**
- `content` 参数接收字符串，但我们可以：
  - 对图像：转换为描述文本、OCR文本、或特征描述
  - 对音频：转换为转录文本、音频特征描述
  - 对视频：提取关键帧描述、字幕文本

#### ✅ **输出格式兼容**
- 后续流程期望的是 `{"tokens": int, "content": str, "chunk_order_index": int}`
- 只要我们的自定义 ChunkingFunc 返回这个格式，后续流程完全兼容

#### ✅ **处理灵活性**
- 我们可以在 ChunkingFunc 内部：
  - 检测输入数据类型（文本、图像路径、音频路径）
  - 调用相应的多模态处理器
  - 将处理结果转换为文本chunks

## 多模态 ChunkingFunc 实现方案

### 方案概述（已升级为 HyperChunker）
```python
class HyperChunker:
    def __call__(
        self,
        tokenizer: Tokenizer,
        content: str,  # JSON: {"asset": {...}, "segments": [...]}
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        overlap_token_size: int = 128,
        max_token_size: int = 1024,
    ) -> list[dict]:
        ...
```

### 具体实现策略（段感知 HyperChunker）

1. **输入协议（与 Parser 对齐）**：
   - `content` 为 JSON 字符串，形如：`{"asset": {...}, "segments": [{segment...}]}`。
   - `segments` 为有序列表，字段来自 `schema.Segment`（`type` 可为 `text/image/table/...`）。

2. **段感知处理**：
   - `text` 段：直接用文本，若为空则填入占位“[空白文本]”。
   - `image` 段：通过 `ImageDescriber` 生成描述文本，拼接 caption/page/bbox；并读取实际图片尺寸（Pillow）。
   - `table` 段：通过 `TableFormatter` 生成 Markdown/文本，拼接标题与必要上下文。
   - 上下文策略：对 `image/table` 段，附加前后各 3 个相邻段文本摘要。

3. **聚合与切分（Token 驱动）**：
   - 普通文本按顺序累加到不超过 `max_token_size` 的窗口，窗口间加入 `overlap_token_size` 的尾部重叠文本（字符近似）。
   - 规则：`image/table` 段必须各自成为独立 chunk，不与其它段合并；若其文本超过上限，在该段内切分为多个 chunk。

4. **输出字段（对齐 HyperRAG + schema.Chunk 最小集）**：
   - 必选：`content`, `tokens`, `chunk_order_index`
   - schema 参考字段：`doc_id`, `segment_ids`, `text`, `token_count`, `order`, `extra`
   - 避免过度设计，保留最小可追溯信息；更多元数据可在后续迭代中加入。

5. **处理器设计**：
   - `ModalityProcessor` 抽象：`to_text(segment, asset, context) -> str`
   - `ImageDescriber(image_dir)`, `TableFormatter(image_dir)`：无模型实现，安全可运行；可替换为带模型版本。

6. **签名对齐**：
   - `HyperChunker.__call__` 与 HyperRAG 的 `chunking_func` 完全一致，无需适配器。

示例（单个输出 chunk）：
```json
{
  "content": "图3：2020-2024年营收趋势...\n尺寸: 1024x768\n上下文: ...",
  "tokens": 742,
  "chunk_order_index": 3,
  "doc_id": "doc-xxx",
  "segment_ids": ["seg-10"],
  "text": "图3：2020-2024年营收趋势...\n尺寸: 1024x768\n上下文: ...",
  "token_count": 742,
  "order": 3,
  "extra": {}
}
```

## MinerUParser 设计与实现

### 设计思路

HyperRAG 的多模态文档处理采用了三层中间表示架构：`Asset -> Segments -> Chunks`。MinerUParser 作为核心组件，负责将 MinerU 的解析结果转换为 HyperRAG 的标准数据结构。

### 核心特性

1. **统一的解析接口**
   - 初始化无需参数，支持动态配置
   - `parse_document(doc_id, doc_path, mineru_dir)` 接口设计
   - 支持多种文档格式：PDF、Office、图像、文本

2. **智能缓存机制**
   - 基于 `content_list.json` 和 `middle.json` 存在性判断解析状态
   - 避免重复解析，提高处理效率
   - 支持增量更新和重新解析

3. **完整的文件格式支持**
   ```python
   OFFICE_FORMATS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
   IMAGE_FORMATS = {".png", ".jpeg", ".jpg", ".bmp", ".tiff", ".tif", ".gif", ".webp"}
   TEXT_FORMATS = {".txt", ".md"}
   ```

4. **自动格式转换**
   - Office 文档通过 LibreOffice 转换为 PDF
   - 文本文件通过 ReportLab 转换为 PDF
   - 图像格式自动转换为 MinerU 兼容格式

### 数据转换策略

#### 1. Asset 层转换
- 提取文档元数据（标题、语言、页数）
- 计算文件哈希值用于版本控制
- 记录 MinerU 输出目录和相关文件路径

#### 2. Segments 层转换
- 支持多种内容类型：text, image, table, equation
- 保留 MinerU 的布局信息和边界框数据
- 维护内容的页面位置和顺序关系

#### 3. 边界框映射机制
通过直接索引实现 `content_list.json` 和 `middle.json` 的精确对应：
```python
def _extract_bbox_from_middle(self, content_index: int, content_item: Dict, middle_data: Dict):
    page_idx = content_item.get("page_idx", 0)
    page_data = middle_data["pdf_info"][page_idx]
    para_blocks = page_data.get("para_blocks", [])
    
    if len(para_blocks) > content_index:
        return para_blocks[content_index].get("bbox")
```

### 线程安全与并发处理

#### 文件级锁机制
```python
@contextmanager
def _get_file_lock(self, doc_output_dir: Path):
    """确保同一个输出目录只有一个线程在处理"""
    lock_key = str(doc_output_dir.absolute())
    
    with self._locks_lock:
        if lock_key not in self._file_locks:
            self._file_locks[lock_key] = threading.Lock()
        file_lock = self._file_locks[lock_key]
    
    try:
        file_lock.acquire()
        yield
    finally:
        file_lock.release()
```

#### 并发安全特性
1. **目录级锁定**：防止多个线程同时处理同一文档的输出目录
2. **双重检查模式**：在锁内再次验证文件解析状态
3. **锁资源管理**：自动清理不再使用的锁对象
4. **异步友好**：支持在异步环境中安全调用

### 异常处理与恢复机制

#### 分类错误处理
```python
def _handle_parsing_error(self, error: Exception, doc_id: str, doc_path: Path, output_dir: Path):
    """根据异常类型提供具体的恢复建议"""
    if isinstance(error, FileNotFoundError):
        # MinerU 未安装或 LibreOffice 缺失
        if "mineru" in error_msg.lower():
            logger.error("Recovery: Install MinerU using 'uv add mineru[core]'")
        elif "libreoffice" in error_msg.lower():
            logger.error("Recovery: Install LibreOffice for Office documents")
    
    elif isinstance(error, subprocess.CalledProcessError):
        # MinerU 命令执行失败
        logger.error(f"Recovery: Check command parameters and document format")
    
    elif isinstance(error, (json.JSONDecodeError, KeyError)):
        # 输出文件损坏或不完整
        logger.error("Recovery: Re-parse the document, output may be corrupted")
```

#### 自动清理机制
- 检测解析失败后的不完整输出文件
- 自动删除损坏的 `content_list.json` 和 `middle.json`
- 为重新解析提供干净的环境
