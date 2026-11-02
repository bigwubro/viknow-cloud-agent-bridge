# 多模态处理指南

## 概述

HyperRAG 通过扩展 HyperRAG 的 `chunking_func` 实现多模态内容处理。系统能够处理文本、图像、音频、视频以及混合内容。

## HyperChunker 段感知聚合（要点）

- 文本跨模态连续聚合：
  - `text` 与 `video_clip` 默认进入“文本缓冲区”，遵循 `max_token_size`，切分处追加 `overlap`。
  - 中途出现 `image/table` 不会打断文本缓冲。
- 模态原子化：
  - `image/table` 段各自独立为 chunk，不与其它合并。
  - 小表格可按阈值并入文本缓冲（`merge_small_tables_threshold_tokens`）。
- 语义分段（可选）：
  - 开启 `respect_semantic_boundaries=True` 时，Markdown 标题或视频场景切换会强制切段。
- 文本标记（可选）：
  - `insert_modal_markers_in_text=True` 可在文本缓冲插入 `[图像]`、`[表格]` 等提示。

### 配置示例

```python
from hyperrag.chunker import HyperChunker

chunker = HyperChunker(
    merge_small_tables_threshold_tokens=32,      # 小表格合入文本
    insert_modal_markers_in_text=True,           # 文本中插入模态提示
    respect_semantic_boundaries=True,            # 遇到 Markdown 标题/scene_break 强制切段
)
```

> 提示：ImageEnhancer 负责为 `image/table` 段生成更强的描述，HyperChunker 只做轻量聚合与分段控制。

## 支持的模态类型

### 1. 文本内容 (Text)

**特点:**
- 直接处理，无需转换
- 支持多语言检测
- 自动关键词提取

**示例:**
```python
text_content = """
深度学习是机器学习的一个子领域，使用多层神经网络。
它在图像识别、自然语言处理等领域取得了突破性进展。
"""
rag.insert(text_content)
```

### 2. 图像内容 (Image)

**检测方式:**
- 文件扩展名：`.jpg`, `.png`, `.gif`, `.bmp`
- MIME 类型检测

**处理流程:**
```
图像文件 → 计算机视觉模型 → 图像描述文本 → 分块处理
```

**示例:**
```python
# 直接使用图像路径
image_path = "./data/chart.png"
rag.insert(image_path)

# 处理结果包含:
# - modality: "image"
# - original_source: "./data/chart.png"
# - content: "[Image content from ./data/chart.png] ..."
```

### 3. 音频内容 (Audio)

**检测方式:**
- 文件扩展名：`.mp3`, `.wav`, `.m4a`, `.flac`
- MIME 类型：`audio/*`

**处理流程:**
```
音频文件 → 语音识别模型 → 转录文本 → 分块处理
```

**示例:**
```python
audio_path = "./recordings/interview.mp3"
rag.insert(audio_path)
```

### 4. 视频内容 (Video)

**检测方式:**
- 文件扩展名：`.mp4`, `.avi`, `.mov`, `.mkv`
- MIME 类型：`video/*`

**处理流程:**
```
视频文件 → 关键帧提取 + 音频提取 → 图像描述 + 转录文本 → 分块处理
```

**示例:**
```python
video_path = "./videos/presentation.mp4"
rag.insert(video_path)
```

### 5. 混合内容 (Mixed)

**检测方式:**
- 包含图像引用：`![alt](image.jpg)`, `<img src="...">`
- 文档中嵌入多媒体内容

**处理策略:**
- 保留原文本结构
- 标记多媒体位置
- 生成统一描述

**示例:**
```python
mixed_content = """
系统架构包含以下组件：

![系统架构图](./architecture.png)

如图所示，系统分为三层：
1. 表示层
2. 业务层  
3. 数据层

<img src="data-flow.jpg" alt="数据流图">
``` 

## 自定义处理器实现

### 图像处理器示例

```python
def _process_image_to_text(image_path: str) -> str:
    """将图像转换为文本描述"""
    
    # 方案1: 使用 BLIP 模型生成描述
    from transformers import BlipProcessor, BlipForConditionalGeneration
    
    processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
    model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")
    
    # 加载图像
    from PIL import Image
    image = Image.open(image_path).convert('RGB')
    
    # 生成描述
    inputs = processor(image, return_tensors="pt")
    out = model.generate(**inputs, max_length=50)
    caption = processor.decode(out[0], skip_special_tokens=True)
    
    # 方案2: 使用 OCR 提取文本
    import easyocr
    reader = easyocr.Reader(['zh', 'en'])
    ocr_results = reader.readtext(image_path)
    ocr_text = " ".join([result[1] for result in ocr_results])
    
    # 组合结果
    description = f"[图像描述: {caption}]"
    if ocr_text.strip():
        description += f" [图像中的文字: {ocr_text}]"
    
    return description
```

### 音频处理器示例

```python
def _process_audio_to_text(audio_path: str) -> str:
    """将音频转换为文本"""
    
    # 使用 Whisper 进行语音识别
    import whisper
    
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, language="zh")
    
    transcript = result["text"]
    
    # 添加元数据
    duration = result.get("duration", 0)
    description = f"[音频转录 (时长: {duration:.1f}秒)]: {transcript}"
    
    return description
```

## 扩展新模态

### 1. 添加检测逻辑

在 `_detect_modality()` 中添加新的检测规则：

```python
def _detect_modality(content: str) -> Dict[str, Any]:
    # ... 现有检测逻辑
    
    # 新模态检测
    if content.endswith('.pdf'):
        return {"type": "pdf", "source_path": content}
    elif content.startswith('http') and 'youtube.com' in content:
        return {"type": "youtube", "url": content}
```

### 2. 实现处理器

```python
def _process_pdf_to_text(pdf_path: str) -> str:
    """处理 PDF 文件"""
    import PyPDF2
    
    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        text = ""
        for page in reader.pages:
            text += page.extract_text()
    
    return f"[PDF内容]: {text}"
```

### 3. 集成到主流程

在 `_process_multimodal_content()` 中添加处理分支：

```python
def _process_multimodal_content(content: str, modality_info: Dict[str, Any]) -> str:
    if modality_info["type"] == "pdf":
        return _process_pdf_to_text(modality_info["source_path"])
    # ... 其他处理逻辑
```

## 高级配置

### 模态优先级

```python
# 在 enhanced_chunking.py 中配置
MODALITY_PRIORITY = {
    "video": 1,    # 最高优先级
    "image": 2,
    "audio": 3,
    "pdf": 4,
    "text": 5      # 默认优先级
}
```

### 处理器选择

```python
# 配置不同的处理器
PROCESSORS = {
    "image": {
        "caption": "blip",      # blip, clip, cogvlm
        "ocr": "easyocr",       # easyocr, paddleocr
        "objects": "yolo"       # yolo, detectron2
    },
    "audio": {
        "transcribe": "whisper", # whisper, wav2vec2
        "language": "auto"       # auto, zh, en
    }
}
```

### 质量控制

```python
def _validate_processing_quality(result: str, modality: str) -> bool:
    """验证处理结果质量"""
    if not result or len(result.strip()) < 10:
        return False
    
    if modality == "image" and "error" in result.lower():
        return False
        
    return True
```

## 性能优化

### 1. 批量处理

```python
def batch_process_images(image_paths: List[str]) -> List[str]:
    """批量处理图像以提高效率"""
    # 使用 GPU 批量推理
    # 复用已加载的模型
    pass
```

### 2. 缓存机制

```python
import hashlib
import pickle

def get_cache_key(file_path: str, modality: str) -> str:
    """生成缓存键"""
    file_stat = os.stat(file_path)
    content = f"{file_path}_{file_stat.st_mtime}_{modality}"
    return hashlib.md5(content.encode()).hexdigest()

def cache_result(key: str, result: str):
    """缓存处理结果"""
    with open(f".cache/{key}.pkl", "wb") as f:
        pickle.dump(result, f)
```

### 3. 异步处理

```python
import asyncio

async def async_process_modality(content: str, modality_info: Dict) -> str:
    """异步处理模态内容"""
    if modality_info["type"] == "image":
        return await asyncio.get_event_loop().run_in_executor(
            None, _process_image_to_text, modality_info["source_path"]
        )
```

## 故障排除

### 常见问题

1. **模态检测失败**
   - 检查文件路径是否正确
   - 验证文件格式是否支持

2. **处理器加载失败**
   - 确认依赖库已安装
   - 检查模型文件是否存在

3. **内存不足**
   - 减少批处理大小
   - 使用模型量化
   - 增加交换空间

### 调试技巧

```python
# 启用调试模式
import logging
logging.basicConfig(level=logging.DEBUG)

# 查看模态检测结果
modality_info = _detect_modality(content)
print(f"检测结果: {modality_info}")

# 测试处理器
try:
    result = _process_image_to_text("test.jpg")
    print(f"处理成功: {result[:100]}...")
except Exception as e:
    print(f"处理失败: {e}")
```