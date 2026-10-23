"""
ImageEnhancer: 基于 VLM 的图片/表格段增强器

使用时机：
- 仅在 MinerUParser 解析完成后、写入 KG 之前调用
- 目标段：type in {"image", "table"}
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from .config_loader import HyperRAGConfig
from .schema import Asset, Segment

logger = logging.getLogger(__name__)


class ImageEnhancer:
    def __init__(
        self,
        vision_model_func: Optional[Any] = None,
        max_concurrent_requests: int = 32,  # 最大并发请求数
    ) -> None:
        self.vision_model_func = vision_model_func
        self.max_concurrent_requests = max_concurrent_requests
        self._semaphore: Optional[asyncio.Semaphore] = None
        self.mineru_mid_path = HyperRAGConfig.mineru_mid_path

    @staticmethod
    def _resolve_mineru_method_dir(asset: Asset) -> Optional[Path]:
        extra = asset.extra or {}
        for key in ("content_list_file", "middle_json_file"):
            candidate = extra.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return Path(candidate).expanduser().resolve().parent

        mineru_output_dir = extra.get("mineru_output_dir")
        if isinstance(mineru_output_dir, str) and mineru_output_dir.strip():
            return Path(mineru_output_dir).expanduser().resolve() / HyperRAGConfig.mineru_mid_path

        return None

    @staticmethod
    def _split_speaker_notes_suffix(text: str) -> tuple[str, str]:
        raw = str(text or "").strip()
        if not raw:
            return "", ""
        m = re.search(r"(?is)(.*?)(#speaker notes:\s*.+)$", raw)
        if not m:
            return raw, ""
        body = str(m.group(1) or "").strip()
        notes = re.sub(r"\s+", " ", str(m.group(2) or "")).strip()
        return body, notes

    async def enhance(
        self,
        asset: Asset,
        segments: List[Segment],
        terminology_prompt_context: Optional[str] = None,
    ) -> List[Segment]:
        """使用视觉模型为图片/表格段生成更强的描述（并发优化版）。

        期望的 vision_model_func 签名：
            async vision_model_func(image_path: str, prompt: str) -> str
            或
            vision_model_func(image_path: str, prompt: str) -> str

        - 第一个参数为图片文件路径。
        - 返回值为中文 caption 文本。
        """
        if not callable(self.vision_model_func):
            return segments

        # 初始化信号量（在运行时的事件循环中创建）
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent_requests)

        # 从 asset.extra 中获取 mineru 输出目录
        mineru_method_dir = self._resolve_mineru_method_dir(asset)
        doc_title = (asset.title or "").strip()
        terminology_prompt_context = (
            str(terminology_prompt_context).strip()
            if terminology_prompt_context is not None
            else ""
        )

        # 第一步：收集所有需要处理的图片/表格段的信息
        image_tasks_info: List[dict] = []  # 存储 {idx, seg, resolved_path, prompt}

        for idx, seg in enumerate(segments):
            if seg.type not in {"image", "table"}:
                continue

            # 构造局部上下文（相邻文本）
            prev_texts: List[str] = []
            next_texts: List[str] = []
            for j in range(max(0, idx - 3), idx):
                if segments[j].text:
                    prev_texts.append(segments[j].text.strip())
            for j in range(idx + 1, min(len(segments), idx + 4)):
                if segments[j].text:
                    next_texts.append(segments[j].text.strip())

            text_context = " \n".join(prev_texts + next_texts)

            # 图片路径定位
            img_path = (
                seg.extra.get("img_path")
                or seg.extra.get("image_path")
                or seg.extra.get("img_relpath")
                or seg.extra.get("path")
            )
            resolved_path: Optional[str] = None
            if isinstance(img_path, str) and img_path:
                p = Path(img_path)
                if not p.is_absolute() and mineru_method_dir is not None:
                    p = mineru_method_dir / p
                resolved_path = str(p)

            if not resolved_path:
                continue
            if not Path(resolved_path).exists():
                logger.warning(
                    'ImageEnhancer: skip missing image asset %s for segment %s: %s',
                    asset.doc_id,
                    seg.segment_id,
                    resolved_path,
                )
                continue

            # TODO(prompt): 将提示词模板抽离为函数/配置，便于统一升级与 A/B 实验
            # TODO(prompt): 为不同类型的文档图片输入设计独立的提示词模板
            # 构造 Prompt（英文提示，但要求中文输出）
            prompt_parts: List[str] = [
                f"You are a helpful assistant that can analyze image and provide captions. Given image is from a document named:{doc_title}. The image can be an ordinary picture, or tables. Analyze the image and provide a detailed technical report in a structured, objective format, based on following requiremnets:",
                "1.For image of tables, extract the data from the table in the image. Directly transcribe the data in Markdown syntax. Ensure all headers are captured accurately.",
                "2.For other images, create a bulleted list of all objects, their key attributes (color, material, state), and their spatial relationships. Then, using only this list, generate a detailed paragraph-long caption that synthesizes these observations.",
                "3. Output language: Chinese.",
                f"Terminology (authoritative, use these meanings when relevant; do not invent beyond them):\n{terminology_prompt_context}"
                if terminology_prompt_context
                else "",
                f"4.Surrounding context for the image, which may help you understand the image, but do not mention any specific details about the context:{text_context}",
            ]
            prompt = "\n".join(p for p in prompt_parts if p)

            image_tasks_info.append(
                {
                    "idx": idx,
                    "seg": seg,
                    "resolved_path": resolved_path,
                    "prompt": prompt,
                }
            )

        # 第二步：并发处理所有图片
        caption_map: dict[int, str] = {}
        if image_tasks_info:
            logger.info(
                f"ImageEnhancer: 开始并发处理 {len(image_tasks_info)} 张图片/表格（并发数：{self.max_concurrent_requests}）"
            )

            # 创建并发任务
            tasks = [self._process_single_image(task_info) for task_info in image_tasks_info]

            # 并发执行所有任务
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 第三步：将结果映射回原始索引
            caption_map: dict[int, str] = {}  # {idx: caption}
            for task_info, result in zip(image_tasks_info, results):
                if isinstance(result, Exception):
                    logger.warning(
                        f"ImageEnhancer: 图片 {task_info['resolved_path'].split('/')[-1]} 处理失败: {result}"
                    )
                    continue
                if result:  # 成功获取到 caption
                    caption_map[task_info["idx"]] = result

        # 第四步：按原始顺序组装增强后的segments
        enhanced: List[Segment] = []
        for idx, seg in enumerate(segments):
            if idx in caption_map:
                # 有VLM描述，合并到文本中
                caption = caption_map[idx]
                seg_source = str((seg.extra or {}).get("source") or "").strip().lower()
                if seg_source == "ppt_visual":
                    # In visual-PPT flow, keep speaker notes as a tail suffix so
                    # chunker can split the full caption once without repeating notes per chunk.
                    base_text, notes_suffix = self._split_speaker_notes_suffix(seg.text)
                    visual_part = f"[视觉要点]\n{caption}".strip()
                    parts = [p for p in [base_text.strip(), visual_part] if p]
                    merged = "\n".join(parts).strip()
                    if notes_suffix:
                        merged = f"{merged}\n{notes_suffix}".strip() if merged else notes_suffix
                    new_extra = dict(seg.extra or {})
                    if merged:
                        new_extra["caption"] = merged
                    seg = seg.model_copy(update={"text": merged or seg.text, "extra": new_extra})
                else:
                    merged = seg.text + "\n[视觉要点]\n" + caption
                    seg = seg.model_copy(update={"text": merged})
            enhanced.append(seg)

        return enhanced

    async def _process_single_image(self, task_info: dict) -> Optional[str]:
        """处理单张图片，返回caption或None（失败时）"""
        resolved_path = task_info["resolved_path"]
        prompt = task_info["prompt"]

        # 使用信号量限制并发数
        async with self._semaphore:
            try:
                # 检查 vision_model_func 是否为异步函数
                is_async = inspect.iscoroutinefunction(self.vision_model_func) or (
                    hasattr(self.vision_model_func, "__call__")
                    and inspect.iscoroutinefunction(self.vision_model_func.__call__)
                )

                if is_async:
                    caption = await self.vision_model_func(resolved_path, prompt)
                else:
                    # 同步函数，在线程池中运行以避免阻塞事件循环
                    caption = await asyncio.to_thread(self.vision_model_func, resolved_path, prompt)

                logger.debug(
                    f"ImageEnhancer: VLM描述图片 {resolved_path.split('/')[-1]}: {caption[:50]}..."
                )
                return caption

            except Exception as e:
                logger.exception(
                    f"ImageEnhancer: VLM处理图片 {resolved_path.split('/')[-1]} 失败: {e}"
                )
                return None
