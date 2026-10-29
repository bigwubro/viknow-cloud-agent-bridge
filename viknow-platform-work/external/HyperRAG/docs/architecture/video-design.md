# 视频与图像处理设计概览（VideoParser 与 ImageEnhancer）

本文档概述 HyperRAG 在“视频解析 + 图像增强”方面的核心设计与当前实现，帮助读者快速把握整体方案与扩展点。

## 目标
- 支持视频文件解析为中间表示（Asset → Segments → Chunks）。
- 在 MinerU 文档解析后，增强图片/表格段的可索引文本。
- 与 HyperRAG 的 `chunking_func` 保持无缝对接。

## 核心组件
- VideoParser（`src/hyperrag/video_parser.py`）
  - 定长分段（`video_segment_length_sec`）。
  - 内置 ASR（faster-whisper，抽象为 `default_asr_model_func`），也支持对象级注入自定义 ASR。
  - 可选 VLM 增强：以“文件路径 + 英文提示词（要求中文输出）”生成描述并合并为“[视觉要点]”。
  - 缓存：`index.json`（assets+segments 快照）、`transcript.json`、`audio.wav`，`frames/` 目录预留（后续扩展）。
- ImageEnhancer（`src/hyperrag/image_enhancer.py`）
  - 仅在 MinerU 输出后、入库前执行；目标段：`image` 与 `table`。
  - 使用 `vlm_model_func(image_path, prompt)` 生成 caption 并合并到“[视觉要点]”。

## 数据流（简）
1. 路由（`HyperRAG.ext_to_parser`）
   - 文档/图片 → MinerUParser；视频 → VideoParser。
2. MinerU 路径：解析 → ImageEnhancer（可选）→ HyperRAG。
3. 视频路径：提取音频 → ASR → 按时间窗聚合为 `video_clip` 段 → 可选 VLM 增强 → HyperRAG。

## 关键配置（`HyperRAGConfig`）
- `vlm_model_func: Optional[Callable]` 多模态描述函数（路径 + prompt → 字符串/字典）。
- `asr_model_func: Optional[Callable]` 自定义 ASR；默认 `default_asr_model_func`（faster-whisper）。
- `video_cache_dir: Optional[str]` 视频缓存根目录。
- `video_segment_length_sec: int` 定长分段时长。
- `video_clip_sample_fps: float` 采样速率（未来用于帧缓存与加速）。

接口规范：
- VLM：`vlm_model_func(path: str, prompt: str) -> str | dict`
- ASR：`asr_model_func(path: str, lang: str | None = None, hotwords: list[str] | None = None) -> list[dict]`

## 缓存布局
```
{video_cache_dir}/{file_stem}/
  ├─ index.json        # 反序列化即得 (Asset, Segments)
  ├─ transcript.json   # ASR 原子结果
  ├─ audio.wav         # 提取的音频（16k PCM）
  └─ frames/           # 代表帧/采样帧（后续扩展）
```

## Chunking 配合
- `HyperChunker` 对 `video_clip` 段增加头部 `[视频片段 HH:MM:SS-HH:MM:SS]` 提升可读性。
- 其余类型复用既有策略（图片/表格文本化、上下文拼接、Token 限制聚合）。

## TODO（保留项）
- 场景分割：接入 PySceneDetect（`video_enable_scene_detect`），输出 `scenes.json`，并将 `scene_id` 写入 `segment.extra`。
- 向量缓存：缓存视频片段/帧的多模态 Embedding，以加速复用。
- 配置与参数打磨：抽出默认语言等细节，支持更细粒度的 `parser_params` 覆盖。
- Prompt 模板抽离：将 Image/Video 的提示词模板抽为函数/配置，便于统一升级和 A/B 实验。
