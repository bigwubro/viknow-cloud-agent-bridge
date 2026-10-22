"""
Contextual Embedder: 为文档块生成上下文信息（Anthropic Contextual Retrieval）

实现 Anthropic 提出的 Contextual Retrieval 技术中的上下文嵌入功能。
在对每个 chunk 进行向量化之前，使用 LLM 为其生成简短的上下文说明，
解释该 chunk 在整个文档中的位置和作用，从而提高检索准确性。

核心思想：
- 传统 RAG 在分块时会丢失上下文信息
- 为每个 chunk 生成 2-3 句话的上下文说明
- 将上下文与原始 chunk 结合后再进行嵌入
- 提高检索时的语义匹配准确度

参考：Anthropic 的 Contextual Retrieval 技术可将检索失败率降低约 49%
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextualEmbedder:
    """
    为文档块生成上下文信息的处理器。
    
    通过 LLM 为每个 chunk 生成简短的上下文说明，解释其在文档中的位置和作用。
    支持批量处理以提高效率，同时提供容错机制。
    
    Attributes:
        llm_func: LLM 调用函数，签名为 (prompt: str) -> str
        max_context_tokens: 生成的上下文最大 token 数限制
    """
    
    def __init__(
        self,
        llm_func: Optional[Callable[[str], str]] = None,
        max_context_tokens: int = 100,
        context_window_chunks: int = 2,
        max_concurrent: int = 16,
    ) -> None:
        """
        初始化 ContextualEmbedder。
        
        Args:
            llm_func: LLM 调用函数，接收 prompt 字符串，返回生成的文本
            max_context_tokens: 生成的上下文说明的最大 token 数
            context_window_chunks: 上下文窗口大小，前后各取多少个相邻 chunk（默认2）
            max_concurrent: 最大并发数，用于限制同时调用 LLM 的数量（默认16）
        """
        self.llm_func = llm_func
        self.max_context_tokens = max_context_tokens
        self.context_window_chunks = context_window_chunks
        self.max_concurrent = max_concurrent
        
        if self.llm_func is None:
            logger.warning(
                "ContextualEmbedder initialized without llm_func. "
                "Contextual embedding will be disabled."
            )

    async def generate_context_for_chunk(
        self,
        chunk_content: str,
        document_context: Optional[str] = None,
        doc_id: Optional[str] = None,
        chunk_order: Optional[int] = None,
        all_chunks: Optional[List[Dict[str, Any]]] = None,
        modality: str = "document",
        begin_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> Optional[str]:
        """
        为单个 chunk 生成上下文说明。
        
        Args:
            chunk_content: chunk 的原始内容
            document_context: 整个文档的概览或摘要（可选，已废弃，使用 all_chunks 代替）
            doc_id: 文档 ID（用于日志和调试）
            chunk_order: chunk 在文档中的顺序（用于生成更精确的上下文）
            all_chunks: 文档的所有 chunks 列表，用于提取相邻 chunks 作为上下文
            modality: 当前 chunk 的来源类型（document/video）
            begin_ts: 视频 chunk 的起始时间（秒）
            end_ts: 视频 chunk 的结束时间（秒）
        
        Returns:
            生成的上下文说明文本，失败时返回 None
        """
        if self.llm_func is None:
            return None
        
        try:
            prompt = self._build_context_prompt(
                chunk_content=chunk_content,
                document_context=document_context,
                chunk_order=chunk_order,
                all_chunks=all_chunks,
                modality=modality,
                begin_ts=begin_ts,
                end_ts=end_ts,
            )
            
            # 检查 llm_func 返回的是否是 coroutine
            context = await self._call_llm(prompt)
            
            if context and isinstance(context, str):
                # 清理生成的上下文，移除多余空白和换行
                context = " ".join(context.strip().split())
                # logger.debug(
                #     f"Generated context for chunk {chunk_order} of doc {doc_id}: "
                #     f"{context[:50]}..."
                # )
                return context
            else:
                logger.warning(
                    f"Invalid context generated for chunk {chunk_order} of doc {doc_id}"
                )
                return None
                
        except Exception as e:
            logger.exception(
                f"Failed to generate context for chunk {chunk_order} of doc {doc_id}: {e}"
            )
            return None

    async def generate_chunk_title_for_chunk(
        self,
        chunk_content: str,
        contextual_context: Optional[str] = None,
        document_context: Optional[str] = None,
        doc_id: Optional[str] = None,
        chunk_order: Optional[int] = None,
        all_chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """
        为单个 chunk 生成用于前端展示的中文标题。

        Args:
            chunk_content: chunk 的原始内容
            contextual_context: 已生成的 chunk 上下文说明（可选）
            document_context: 整个文档的概览或摘要（可选）
            doc_id: 文档 ID（用于日志和调试）
            chunk_order: chunk 在文档中的顺序（用于生成更精确的标题）
            all_chunks: 文档的所有 chunks 列表，用于提取相邻 chunks 作为上下文

        Returns:
            不超过 15 个字符的中文标题，失败时返回 None
        """
        if self.llm_func is None:
            return None

        try:
            prompt = self._build_chunk_title_prompt(
                chunk_content=chunk_content,
                contextual_context=contextual_context,
                document_context=document_context,
                chunk_order=chunk_order,
                all_chunks=all_chunks,
            )
            result = await self._call_llm(prompt)
            chunk_title = self._normalize_chunk_title(result)
            if chunk_title:
                return chunk_title

            logger.warning(
                f"Invalid chunk title generated for chunk {chunk_order} of doc {doc_id}"
            )
            return None
        except Exception as e:
            logger.exception(
                f"Failed to generate chunk title for chunk {chunk_order} of doc {doc_id}: {e}"
            )
            return None

    async def _call_llm(self, prompt: str) -> Any:
        result = self.llm_func(prompt)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def _build_chunk_title_prompt(
        self,
        chunk_content: str,
        contextual_context: Optional[str] = None,
        document_context: Optional[str] = None,
        chunk_order: Optional[int] = None,
        all_chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        prompt_parts = []

        if document_context:
            prompt_parts.append(f"<文档摘要>\n{document_context}\n</文档摘要>")

        if all_chunks and chunk_order is not None and chunk_order >= 0:
            surrounding_context = self._extract_surrounding_chunks(
                all_chunks=all_chunks,
                current_order=chunk_order,
                window_size=self.context_window_chunks,
            )
            if surrounding_context:
                prompt_parts.append(f"<相邻片段>\n{surrounding_context}\n</相邻片段>")

        if contextual_context:
            prompt_parts.append(f"<局部上下文说明>\n{contextual_context}\n</局部上下文说明>")

        prompt_parts.append(f"<chunk>\n{chunk_content}\n</chunk>")
        prompt_parts.append(
            "请根据以上内容生成这个 chunk 的标题，用于知识库索引列表的前端展示。"
            "标题必须使用中文，准确概括当前 chunk 的核心主题，最多 15 个汉字。"
            "只输出标题文本，不要输出引号、前缀、解释、换行或 Markdown。"
        )
        return "\n".join(prompt_parts)

    @staticmethod
    def _normalize_chunk_title(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            value = value.get("chunk_title")
        if not isinstance(value, str):
            return None

        title = " ".join(value.strip().split())
        if title.startswith("{") and title.endswith("}"):
            try:
                parsed = json.loads(title)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                title = str(parsed.get("chunk_title") or "")
                title = " ".join(title.strip().split())

        title = title.splitlines()[0].strip() if title else ""
        for prefix in ("chunk_title:", "chunk_title：", "标题:", "标题："):
            if title.startswith(prefix):
                title = title[len(prefix) :].strip()
                break
        title = title.strip("`\"'“”‘’")
        title = " ".join(title.split())
        if not title or not any("\u4e00" <= char <= "\u9fff" for char in title):
            return None
        return title[:15]
    
    def _build_context_prompt(
        self,
        chunk_content: str,
        document_context: Optional[str] = None,
        chunk_order: Optional[int] = None,
        all_chunks: Optional[List[Dict[str, Any]]] = None,
        modality: str = "document",
        begin_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> str:
        """
        构建生成上下文的 LLM prompt。
        
        Args:
            chunk_content: chunk 内容
            document_context: 文档整体摘要（由 LLM 生成的 2-3 句话总结）
            chunk_order: chunk 顺序
            all_chunks: 文档的所有 chunks 列表，用于提取相邻 chunks
            modality: 当前 chunk 的来源类型（document/video）
            begin_ts: 视频 chunk 的起始时间（秒）
            end_ts: 视频 chunk 的结束时间（秒）
        
        Returns:
            完整的 LLM prompt 字符串
        """
        
        prompt_parts = []
        
        # 1. 添加文档级别的摘要（如果有）
        if document_context:
            prompt_parts.append(f"<文档摘要>\n{document_context}\n</文档摘要>")
        
        # 2. 添加相邻 chunks 的局部上下文（如果有）
        if all_chunks and chunk_order is not None and chunk_order >= 0:
            surrounding_context = self._extract_surrounding_chunks(
                all_chunks=all_chunks,
                current_order=chunk_order,
                window_size=self.context_window_chunks,
            )
            if surrounding_context:
                prompt_parts.append(f"<相邻片段>\n{surrounding_context}\n</相邻片段>")
        
        # 主要的任务说明（按模态区分）
        chunk_index = (chunk_order + 1) if chunk_order is not None and chunk_order >= 0 else None
        normalized_modality = "video" if modality == "video" else "document"
        if normalized_modality == "video":
            time_hint = self._build_video_time_hint(begin_ts=begin_ts, end_ts=end_ts)
            prompt_parts.append(
                "以下文本块（chunk）来自视频内容。"
                f"{time_hint}"
                "请为它生成一段简短的上下文说明（2-3句话），"
                "解释这个视频片段在整体视频中的位置、主要画面或动作、以及核心主题。"
                "请在输出中明确出现该片段的时间位置（如“位于视频的90秒到120秒”），不要使用“开头/中段/结尾”等模糊词。"
                "这段上下文说明将用于帮助后续的信息检索，使检索系统能够更好地理解这个视频片段的背景和含义。"
            )
        else:
            if chunk_index is not None:
                prompt_parts.append(
                    f"以下文本块（chunk）是文档中的第 {chunk_index} 个片段。"
                    "请为它生成一段简短的上下文说明（2-3句话），"
                    "解释这个片段在整个文档中的位置、所属部分、以及主要讨论的主题。"
                    "请在输出中明确出现“第 "
                    f"{chunk_index}"
                    " 个片段”这个表述，不要使用“开头/中段/结尾”等模糊词。"
                    "这段上下文说明将用于帮助后续的信息检索，使检索系统能够更好地理解这个片段的背景和含义。"
                )
            else:
                prompt_parts.append(
                    "以下文本块（chunk）来自文档内容。"
                    "请为它生成一段简短的上下文说明（2-3句话），"
                    "解释这个片段在整个文档中的位置、所属部分、以及主要讨论的主题。"
                    "这段上下文说明将用于帮助后续的信息检索，使检索系统能够更好地理解这个片段的背景和含义。"
                )
        
        # 添加 chunk 内容
        prompt_parts.append(f"<chunk>\n{chunk_content}\n</chunk>\n")
        
        # 输出格式要求
        prompt_parts.append(
            "请直接输出上下文说明，不要包含任何前缀（如'上下文说明：'）或其他解释。"
            f"上下文说明应该简洁明了，不超过 {self.max_context_tokens} 个字。"
        )
        
        return "\n".join(prompt_parts)

    @staticmethod
    def _format_seconds(seconds: Optional[float]) -> Optional[str]:
        if seconds is None:
            return None
        sec_val = float(seconds)
        if sec_val.is_integer():
            return str(int(sec_val))
        return f"{sec_val:.2f}".rstrip("0").rstrip(".")

    def _build_video_time_hint(
        self,
        begin_ts: Optional[float],
        end_ts: Optional[float],
    ) -> str:
        begin = self._format_seconds(begin_ts)
        end = self._format_seconds(end_ts)
        if begin is not None and end is not None:
            return f"该片段位于视频的 {begin} 秒到 {end} 秒。"
        if begin is not None:
            return f"该片段从视频第 {begin} 秒开始。"
        if end is not None:
            return f"该片段截至视频第 {end} 秒。"
        return "当前未提供精确时间戳，请结合相邻片段判断视频时序位置。"
    
    def _extract_surrounding_chunks(
        self,
        all_chunks: List[Dict[str, Any]],
        current_order: int,
        window_size: int,
    ) -> Optional[str]:
        """
        提取当前 chunk 周围的相邻 chunks 作为上下文。
        
        Args:
            all_chunks: 文档的所有 chunks 列表
            current_order: 当前 chunk 的顺序索引
            window_size: 窗口大小，前后各取多少个 chunk
        
        Returns:
            拼接后的相邻 chunks 内容，如果没有相邻 chunks 则返回 None
        """
        if not all_chunks or window_size <= 0:
            return None
        
        # 找到当前 chunk 的索引位置
        current_idx = None
        for idx, chunk in enumerate(all_chunks):
            if chunk.get("order") == current_order:
                current_idx = idx
                break
        
        if current_idx is None:
            return None
        
        # 计算窗口范围（前后各取 window_size 个）
        start_idx = max(0, current_idx - window_size)
        end_idx = min(len(all_chunks), current_idx + window_size + 1)
        
        # 提取相邻 chunks，包含当前 chunk 本身
        surrounding_chunks = []
        for idx in range(start_idx, end_idx):
            chunk = all_chunks[idx]
            chunk_content = chunk.get("content", "")
            chunk_order = chunk.get("order", idx)
                
            # 试图提取原始内容
            try:
                chunk_json = json.loads(chunk_content)
                chunk_content = chunk_json.get("content", "")
            except Exception:
                pass
                
            surrounding_chunks.append(f"[片段 {chunk_order + 1}]: {chunk_content}")    
        
        if not surrounding_chunks:
            return None
        
        return "\n".join(surrounding_chunks)

    async def add_context_to_chunks(
        self,
        chunks: List[Dict[str, Any]],
        document_context: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        批量为 chunks 添加上下文信息（并发执行，带并发限制）。
        
        Args:
            chunks: chunk 字典列表，每个字典应包含 'content', 'doc_id', 'order' 等字段
            document_context: 整个文档的概览（已废弃，使用相邻 chunks 代替）
        
        Returns:
            更新后的 chunks 列表。每个 chunk 的 content 会添加生成的上下文，
            metadata 会添加 LLM 生成的 chunk_title。
        """
        if self.llm_func is None:
            logger.info("ContextualEmbedder: llm_func not set, skipping context generation")
            return chunks
        
        logger.info(
            f"ContextualEmbedder: generating contexts for {len(chunks)} chunks "
            f"(max concurrent: {self.max_concurrent})"
        )
        
        # 创建信号量来限制并发数
        semaphore = asyncio.Semaphore(self.max_concurrent)
        
        async def process_single_chunk(chunk: Dict[str, Any]) -> None:
            """处理单个 chunk，为其生成并添加上下文（带并发限制）"""
            async with semaphore:  # 获取信号量，限制并发数
                try:
                    # 提取 chunk 信息
                    original_content = chunk.get("content", "")
                    doc_id = chunk.get("doc_id", "unknown")
                    chunk_order = chunk.get("order", -1)
                    metadata = chunk.get("metadata") or {}
                    chunk_type = str(metadata.get("chunk_type", "")).lower()
                    modality = "video" if chunk_type == "video" else "document"
                    begin_ts = metadata.get("begin_ts")
                    end_ts = metadata.get("end_ts")
                    
                    # 生成上下文，传递所有 chunks 以便提取相邻内容
                    context = await self.generate_context_for_chunk(
                        chunk_content=original_content,
                        document_context=document_context,
                        doc_id=doc_id,
                        chunk_order=chunk_order,
                        all_chunks=chunks,  # 传递所有 chunks
                        modality=modality,
                        begin_ts=begin_ts,
                        end_ts=end_ts,
                    )
                    chunk_title = await self.generate_chunk_title_for_chunk(
                        chunk_content=original_content,
                        contextual_context=context,
                        document_context=document_context,
                        doc_id=doc_id,
                        chunk_order=chunk_order,
                        all_chunks=chunks,
                    )

                    # 如果成功生成上下文，则添加到json字符串的context键下
                    if context:
                        try:
                            content = json.loads(original_content)
                        except Exception:
                            content = {"content": original_content}
                        content = {"context": context} | content
                        contextual_content = json.dumps(content, ensure_ascii=False)
                        chunk["content"] = contextual_content

                    if context or chunk_title:
                        metadata = dict(metadata)
                    if context:
                        metadata["context"] = context
                    if chunk_title:
                        metadata["chunk_title"] = chunk_title
                    if context or chunk_title:
                        chunk["metadata"] = metadata
                    
                except Exception as e:
                    logger.exception(f"Error processing chunk: {e}")
        
        # 并发处理所有 chunks（受信号量限制）
        await asyncio.gather(*[process_single_chunk(chunk) for chunk in chunks])
        
        return chunks
