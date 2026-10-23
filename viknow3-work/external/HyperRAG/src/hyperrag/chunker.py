"""
HyperChunker: 段感知的 KG 分块实现（精简版）

设计目标：
- 直接作为 KG 分块器使用。
- 输入优先为 JSON 字符串，包含 `asset` 与有序 `segments`（来自 MinerUParser）。
- 图片/表格等模态的增强已在外部完成（参见 ImageEnhancer），本模块仅进行轻量文本拼接与 token 限制聚合。
- 统一输出路径，集中构建 chunk，提升可读性与可维护性。
- 生成的 chunk 字段参考 `schema.Chunk` 的最小集合：
  - content, tokens, chunk_order_index
  - 以及最小必要的 schema 字段：text, token_count, order, doc_id, segment_ids, extra
- 支持 Contextual Retrieval（上下文嵌入）：可选为每个 chunk 生成上下文说明，提高检索准确性
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from hyperrag.schema import Segment
from hyperrag.utils.segment_tools import build_video_chunk_display_metadata, build_video_segments

logger = logging.getLogger(__name__)


def compute_mdhash_id(content: str, prefix: str = "") -> str:
    h = hashlib.md5(content.encode("utf-8")).hexdigest()
    return f"{prefix}{h}"


Tokenizer = Any

# 导入 ContextualEmbedder
try:
    from .contextual_embedder import ContextualEmbedder
except ImportError:  # pragma: no cover
    ContextualEmbedder = None  # type: ignore


class HyperChunker:
    """
    段感知 KG 分块器。

    __call__ 签名：
    (tokenizer, content: str, split_by_character=None, split_by_character_only=False,
     overlap_token_size=128, max_token_size=1024) -> List[dict]

    可选支持 Contextual Retrieval（上下文嵌入）：
    - 为每个 chunk 生成简短的上下文说明
    - 提高检索时的准确性

    """

    def __init__(
        self,
        merge_small_tables_threshold_tokens: Optional[int] = None,
        insert_modal_markers_in_text: bool = False,
        respect_semantic_boundaries: bool = True,
        enable_modal_boundary_flush: bool = True,
        enable_modal_replay: bool = False,
        modal_flush_min_fill_ratio: float = 0.5,
        modal_flush_min_fill_floor_tokens: int = 64,
        modal_flush_replay_max_tokens: int = 128,
        enable_contextual_embeddings: bool = True,
        contextual_llm_func: Optional[Callable] = None,
        contextual_max_tokens: int = 100,
        contextual_window_chunks: int = 2,
        contextual_max_concurrent: int = 16,
        enable_semantic_splitting: bool = True,
        continuous_text_target_tokens: int = 448,
    ) -> None:
        self.merge_small_tables_threshold_tokens = merge_small_tables_threshold_tokens
        self.insert_modal_markers_in_text = insert_modal_markers_in_text
        self.respect_semantic_boundaries = respect_semantic_boundaries
        self.enable_modal_boundary_flush = enable_modal_boundary_flush
        self.enable_modal_replay = enable_modal_replay
        self.modal_flush_min_fill_ratio = max(0.0, min(1.0, float(modal_flush_min_fill_ratio)))
        self.modal_flush_min_fill_floor_tokens = max(1, int(modal_flush_min_fill_floor_tokens))
        self.modal_flush_replay_max_tokens = max(0, int(modal_flush_replay_max_tokens))
        self.enable_semantic_splitting = enable_semantic_splitting
        self.continuous_text_target_tokens = max(1, int(continuous_text_target_tokens))

        # 初始化语义分割器（基于 RecursiveCharacterTextSplitter）
        # 使用中英文友好的分隔符序列：段落 -> 句子 -> 短语 -> 字符
        self.text_splitter: Optional[RecursiveCharacterTextSplitter] = None
        if self.enable_semantic_splitting:
            self.text_splitter = RecursiveCharacterTextSplitter(
                separators=[
                    "\n# ",  # Markdown标题
                    "\n\n",  # 段落分隔
                    "\n",  # 行分隔
                    "。",  # 中文句号
                    "！",  # 中文感叹号
                    "？",  # 中文问号
                    ".",  # 英文句号
                    "!",  # 英文感叹号
                    "?",  # 英文问号
                    "；",  # 中文分号
                    ";",  # 英文分号
                    "，",  # 中文逗号
                    ",",  # 英文逗号
                    " ",  # 空格
                    "",  # 字符级别（兜底）
                ],
                chunk_size=512,  # 默认大小，实际使用时会被 max_token_size 覆盖
                chunk_overlap=0,  # 默认重叠，实际使用时会被 overlap_token_size 覆盖
                length_function=len,  # 使用字符长度，会在调用时配合 tokenizer 使用
                is_separator_regex=False,
            )
            logger.info("Semantic text splitter initialized with Chinese and English separators")

        # Contextual Retrieval 配置
        self.enable_contextual_embeddings = enable_contextual_embeddings
        self.contextual_embedder: Optional[Any] = None

        if self.enable_contextual_embeddings:
            if ContextualEmbedder is None:
                logger.warning(
                    "ContextualEmbedder not available, contextual embeddings will be disabled"
                )
                self.enable_contextual_embeddings = False
            elif contextual_llm_func is None:
                logger.warning(
                    "contextual_llm_func not provided, contextual embeddings will be disabled"
                )
                self.enable_contextual_embeddings = False
            else:
                self.contextual_embedder = ContextualEmbedder(
                    llm_func=contextual_llm_func,
                    max_context_tokens=contextual_max_tokens,
                    context_window_chunks=contextual_window_chunks,
                    max_concurrent=contextual_max_concurrent,
                )
                logger.info(
                    "ContextualEmbedder enabled with max_context_tokens=%s, window_chunks=%s",
                    contextual_max_tokens,
                    contextual_window_chunks,
                )

    def __deepcopy__(self, memo):
        """
        让 HyperChunker 可被 copy.deepcopy。

        关键点：
        - chunk_meta_storage / asset_status_storage 可能持有 asyncpg pool/connection 等不可 pickle 对象
        - contextual_embedder/llm_func 也可能持有不可拷贝闭包/客户端
        因此 deepcopy 时做“浅拷贝 + 剥离不可拷贝字段”，仅用于序列化/日志，不影响运行时 chunking。
        """
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result

        # 默认浅复制所有属性，遇到不可拷贝字段则置空
        for k, v in self.__dict__.items():
            if k in {"contextual_embedder"}:
                setattr(result, k, None)
            elif k in {"text_splitter"}:
                # splitter 内部状态不重要，且可能包含不可拷贝对象
                setattr(result, k, None)
            else:
                # 浅赋值，避免递归 deepcopy
                setattr(result, k, v)

        return result

    # KG 分块实现
    async def __call__(
        self,
        tokenizer: Tokenizer,
        content: str,
        split_by_character: Optional[str] = None,
        split_by_character_only: bool = False,
        overlap_token_size: int = 128,
        max_token_size: int = 1024,
    ) -> List[Dict[str, Any]]:
        # 统一入口：若 content 是约定的 Asset+Segments JSON，则段感知处理；否则基础分块。
        parsed: Optional[Dict[str, Any]] = None
        is_sop_video = False
        try:
            maybe = json.loads(content)
            # 支持 SOP 视频格式 (sop_full) 和普通格式 (asset + segments)
            if isinstance(maybe, dict):
                if "asset" in maybe and "segments" in maybe:
                    parsed = maybe
                    logger.info(
                        f"HyperChunker: received asset doc_id={parsed['asset'].get('doc_id', 'unknown')}"
                    )
                elif "sop_full" in maybe:
                    # SOP 视频格式
                    parsed = maybe["sop_full"]
                    is_sop_video = True
                    logger.info(
                        f"HyperChunker: received sop_full doc_id={parsed.get('metadata', {}).get('sop_id', 'unknown')}"
                    )
        except Exception:
            parsed = None

        if parsed is None:
            logger.info(
                f"HyperChunker: plain text fallback. max_token_size={max_token_size}, overlap={overlap_token_size}"
            )
            base_chunks = self._chunk_plain_text_by_token_size(
                tokenizer=tokenizer,
                content=content,
                overlap_token_size=overlap_token_size,
                max_token_size=max_token_size,
            )
            # 统一通过 _make_chunk 标准化字段
            normalized: List[Dict[str, Any]] = []
            for ch in base_chunks:
                kwargs = dict(ch)
                if "doc_id" not in kwargs:
                    kwargs["doc_id"] = ""
                if "file_path" not in kwargs:
                    kwargs["file_path"] = kwargs["doc_id"]
                normalized.append(self._make_chunk(**kwargs))
            return normalized

        # ---------- 段感知路径 ----------
        # 检查是否是 SOP 视频格式
        if is_sop_video:
            metadata = parsed.get("metadata", {})
            sources = metadata.get("sources") or []
            primary_source = next(
                (
                    source
                    for source in sources
                    if isinstance(source, dict) and source.get("source_type") == "video"
                ),
                {},
            )
            doc_id = str(metadata.get("sop_id") or primary_source.get("source_id") or "")
            asset = {
                "doc_id": doc_id,
                "doc_type": "sop_video",
                "title": metadata.get("title", ""),
                "modality": "video",
            }
            segments = build_video_segments(
                asset_id=doc_id,
                sop_full_data=parsed,
                source_parser="sop_video",
            )
            logger.info(f"HyperChunker (SOP): doc_id={doc_id} segments={len(segments)}")
        else:
            asset = parsed["asset"]
            segments = list(sorted(parsed.get("segments", []), key=lambda s: s.get("order", 0)))
            logger.info(
                f"HyperChunker: doc_id={asset.get('doc_id', 'unknown')} segments={len(segments)}"
            )

        # 根据 token 上限进行窗口聚合（文本跨模态连续聚合，模态原子化）
        chunks = self._aggregate_by_token_limit(
            tokenizer=tokenizer,
            doc_id=str(asset.get("doc_id", "")),
            asset=dict(asset or {}),
            segments=segments,
            overlap_token_size=overlap_token_size,
            max_token_size=max_token_size,
        )
        logger.info(
            f"HyperChunker: generated {len(chunks)} chunks for doc_id={asset.get('doc_id', 'unknown')}"
        )

        # 应用上下文嵌入（如果启用）
        if self.enable_contextual_embeddings and self.contextual_embedder is not None:
            # 仅对文档和视频类型应用上下文嵌入
            modality = asset.get("modality", "document")
            if modality not in {"document", "video"}:
                return chunks
            logger.info(f"HyperChunker: applying contextual embeddings to {len(chunks)} chunks")
            # 构建文档级别的上下文（从 segments 中提取）
            doc_context = await self._build_document_context(segments)
            chunks = await self.contextual_embedder.add_context_to_chunks(
                chunks=chunks,
                document_context=doc_context,
            )
            logger.info(f"HyperChunker: applied contextual embeddings to {len(chunks)} chunks")

        return chunks

    # 聚合实现：按 segment 顺序累加，超过阈值则切分；提供简单的 token 级重叠
    def _aggregate_by_token_limit(
        self,
        tokenizer: Tokenizer,
        doc_id: str,
        asset: Dict[str, Any],
        segments: List[Dict[str, Any]],
        overlap_token_size: int,
        max_token_size: int,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        text_max_token_size = min(max_token_size, self.continuous_text_target_tokens)

        seg_map: Dict[str, Dict[str, Any]] = {
            str(s.get("segment_id")): s for s in (segments or []) if s.get("segment_id")
        }

        # 文本缓冲（仅收集文本类段）
        buffer_text_parts: List[str] = []
        buffer_segment_ids: List[str] = []
        buffer_token_count = 0
        chunk_index = 0
        last_emitted_text_chunk_content = ""
        last_emitted_text_chunk_segment_ids: List[str] = []
        current_section_index = 0
        last_emitted_text_chunk_section_index: Optional[int] = None
        buffer_starts_with_short_section_marker = False
        pending_empty_segment_ids: List[str] = []
        # Cache markdown table header by independent table key.
        table_header_cache: Dict[str, str] = {}

        def encode(s: str) -> List[int]:
            return self._safe_encode(tokenizer, s)

        def treat_as_text(
            seg_type: str, seg_tokens: int, table_has_trusted_header: bool = True
        ) -> bool:
            # 确定段落是否作为文本处理
            if seg_type in {"text", "heading", "video_clip"}:
                return True
            if seg_type == "table" and self.merge_small_tables_threshold_tokens is not None:
                return table_has_trusted_header and seg_tokens <= int(
                    self.merge_small_tables_threshold_tokens
                )
            return False

        def starts_new_section(seg: Dict[str, Any], text: str) -> bool:
            if str(seg.get("type") or "").lower() == "heading":
                return True
            return bool(re.match(r"^\s{0,3}\\?#{1,6}(?:\s|$)", text))

        def extract_last_md_table_header(text: str) -> Optional[str]:
            lines = text.splitlines()
            last_header: Optional[str] = None
            for idx in range(len(lines) - 1):
                if self._looks_like_md_table_row(
                    lines[idx]
                ) and self._looks_like_md_table_separator(lines[idx + 1]):
                    last_header = f"{lines[idx]}\n{lines[idx + 1]}"
            return last_header

        def get_segment_table_key(seg: Dict[str, Any]) -> str:
            loc = seg.get("location") or {}
            extra = seg.get("extra") or {}

            # Preferred explicit key from Excel parser.
            table_id = loc.get("table_id") or (
                extra.get("excel_table_id") if isinstance(extra, dict) else None
            )
            if table_id is not None:
                return str(table_id)

            # Backward-compatible fallback for old index: page + column span scope.
            page = loc.get("page")
            col_start = loc.get("col_start")
            col_end = loc.get("col_end")
            if page is not None and col_start is not None and col_end is not None:
                return f"page_{page}_cols_{col_start}_{col_end}"

            col_len = loc.get("col_len")
            if page is not None and col_len is not None:
                return f"page_{page}_cols_legacy_{col_len}"
            if page is not None:
                return f"page_{page}"
            return "__default__"

        def get_buffer_table_key() -> str:
            # Use last segment in buffer as current table context.
            if not buffer_segment_ids:
                return "__default__"
            last_sid = str(buffer_segment_ids[-1])
            seg = seg_map.get(last_sid) or {}
            return get_segment_table_key(seg)

        def ensure_md_table_header_for_continuation(text: str, table_key: str) -> str:
            lines = text.splitlines()
            if not lines:
                return text

            first = lines[0]
            second = lines[1] if len(lines) > 1 else ""
            is_continuation = self._looks_like_md_table_row(
                first
            ) and not self._looks_like_md_table_separator(second)
            header = table_header_cache.get(table_key)
            if is_continuation and header:
                return f"{header}\n{text}"
            return text

        def extract_tail_text_with_token_budget(source: str, budget_tokens: int) -> str:
            if budget_tokens <= 0 or not source:
                return ""

            all_tokens = encode(source)
            if not all_tokens:
                return ""
            if len(all_tokens) <= budget_tokens:
                return source

            # Search on the original Unicode string so replay text never starts
            # from the middle of a merged token/byte sequence.
            left = 0
            right = len(source)
            best_start = len(source)
            while left <= right:
                mid = (left + right) // 2
                suffix = source[mid:]
                suffix_tokens = len(encode(suffix))
                if suffix_tokens > budget_tokens:
                    left = mid + 1
                else:
                    best_start = mid
                    right = mid - 1

            suffix = source[best_start:]
            while suffix and len(encode(suffix)) > budget_tokens:
                suffix = suffix[1:]
            return suffix

        def apply_stage_a_replay_fill(
            text_chunk: str,
            tokens_val: int,
            max_tokens: int,
        ) -> tuple[str, int]:
            if tokens_val <= 0:
                return text_chunk, tokens_val

            target_tokens = min(
                max_tokens,
                max(
                    self.modal_flush_min_fill_floor_tokens,
                    int(max_tokens * self.modal_flush_min_fill_ratio),
                ),
            )
            if tokens_val >= target_tokens:
                return text_chunk, tokens_val
            if (
                not last_emitted_text_chunk_content
                or last_emitted_text_chunk_section_index != current_section_index
                or self.modal_flush_replay_max_tokens <= 0
            ):
                return text_chunk, tokens_val

            replay_budget = min(
                self.modal_flush_replay_max_tokens,
                target_tokens - tokens_val,
                text_max_token_size - tokens_val,
            )
            if replay_budget <= 0:
                return text_chunk, tokens_val

            replay_text = extract_tail_text_with_token_budget(
                last_emitted_text_chunk_content,
                replay_budget,
            ).strip()
            if not replay_text:
                return text_chunk, tokens_val

            filled = f"{replay_text}\n\n{text_chunk}"
            filled_tokens = len(encode(filled))
            if filled_tokens <= max_tokens:
                return filled, filled_tokens

            # Fallback safety: trim replay head until within max tokens.
            trimmed_replay = replay_text
            while trimmed_replay and filled_tokens > max_tokens:
                trimmed_replay = trimmed_replay[1:]
                if not trimmed_replay.strip():
                    break
                filled = f"{trimmed_replay}\n\n{text_chunk}"
                filled_tokens = len(encode(filled))

            if not trimmed_replay.strip():
                return text_chunk, tokens_val
            return filled, filled_tokens

        def flush_text_chunk(*, modal_boundary: bool = False) -> None:
            # 清空当前缓冲，生成chunk
            nonlocal \
                buffer_text_parts, \
                buffer_segment_ids, \
                buffer_token_count, \
                chunk_index, \
                last_emitted_text_chunk_content, \
                last_emitted_text_chunk_segment_ids, \
                last_emitted_text_chunk_section_index, \
                buffer_starts_with_short_section_marker
            if not buffer_text_parts:
                return
            combined = "".join(t for t in buffer_text_parts if t)
            table_key = get_buffer_table_key()
            combined = ensure_md_table_header_for_continuation(combined, table_key)

            header = extract_last_md_table_header(combined)
            if header:
                table_header_cache[table_key] = header

            tokens_val = len(encode(combined))
            if tokens_val <= 0:
                buffer_text_parts, buffer_segment_ids, buffer_token_count = [], [], 0
                buffer_starts_with_short_section_marker = False
                return

            replay_filled = False
            replay_segment_ids: List[str] = []
            if modal_boundary and self.enable_modal_boundary_flush and self.enable_modal_replay:
                original_combined = combined
                combined, tokens_val = apply_stage_a_replay_fill(
                    combined,
                    tokens_val,
                    text_max_token_size,
                )
                replay_filled = combined != original_combined
                if replay_filled:
                    replay_segment_ids = list(last_emitted_text_chunk_segment_ids)

            def build_emitted_segment_ids(*, include_replay_ids: bool) -> List[str]:
                merged_ids = list(buffer_segment_ids)
                if include_replay_ids and replay_segment_ids:
                    merged_ids = list(replay_segment_ids) + merged_ids
                return list(dict.fromkeys(merged_ids))

            # 如果拼接的buffer内容超过token限制，使用语义分割
            if tokens_val > text_max_token_size and self.enable_semantic_splitting:
                split_chunks = self._semantic_split_text(
                    tokenizer, combined, text_max_token_size, overlap_token_size
                )
                for split_idx, split_chunk in enumerate(split_chunks):
                    split_tokens = len(encode(split_chunk))
                    unique_ids = build_emitted_segment_ids(
                        include_replay_ids=(replay_filled and split_idx == 0)
                    )
                    results.append(
                        self._make_chunk(
                            content=split_chunk,
                            tokens=split_tokens,
                            chunk_order_index=chunk_index,
                            file_path=doc_id,
                            doc_id=doc_id,
                            segment_ids=unique_ids,
                            order=chunk_index,
                            metadata=self._build_chunk_metadata(
                                asset=asset,
                                seg_map=seg_map,
                                segment_ids=unique_ids,
                                edited=replay_filled,
                            ),
                        )
                    )
                    last_emitted_text_chunk_content = split_chunk
                    last_emitted_text_chunk_segment_ids = list(unique_ids)
                    last_emitted_text_chunk_section_index = current_section_index
                    chunk_index += 1
            else:
                # 直接创建 chunk
                unique_ids = build_emitted_segment_ids(include_replay_ids=replay_filled)
                results.append(
                    self._make_chunk(
                        content=combined,
                        tokens=tokens_val,
                        chunk_order_index=chunk_index,
                        file_path=doc_id,
                        doc_id=doc_id,
                        segment_ids=unique_ids,
                        order=chunk_index,
                        metadata=self._build_chunk_metadata(
                            asset=asset,
                            seg_map=seg_map,
                            segment_ids=unique_ids,
                            edited=replay_filled,
                        ),
                    )
                )
                last_emitted_text_chunk_content = combined
                last_emitted_text_chunk_segment_ids = list(unique_ids)
                last_emitted_text_chunk_section_index = current_section_index
                chunk_index += 1
            buffer_text_parts, buffer_segment_ids, buffer_token_count = [], [], 0

        def ensure_space_for(tokens_to_add: int) -> None:
            if buffer_token_count > 0 and buffer_token_count + tokens_to_add > text_max_token_size:
                flush_text_chunk()

        def take_pending_empty_segment_ids() -> List[str]:
            nonlocal pending_empty_segment_ids
            pending = pending_empty_segment_ids
            pending_empty_segment_ids = []
            return pending

        def append_text_part(part: str, seg_id: str) -> None:
            nonlocal buffer_token_count, buffer_starts_with_short_section_marker
            part_tokens = len(encode(part))
            ensure_space_for(part_tokens)
            buffer_text_parts.append(part)
            buffer_segment_ids.append(seg_id)
            buffer_token_count += part_tokens
            buffer_starts_with_short_section_marker = False

        asset_modality = str((asset or {}).get("modality") or "").lower()

        for i, seg in enumerate(segments):
            seg_type = seg.get("type", "text")
            seg_id = seg.get("segment_id", f"seg-{i}")
            raw_text = str(seg.get("text") or "")
            text, table_has_trusted_header = (
                self._table_content(seg, raw_text) if seg_type == "table" else (raw_text, True)
            )
            seg_tokens = len(encode(text))

            if not text.strip() and not (asset_modality == "video" and seg_type == "video_clip"):
                if self.respect_semantic_boundaries and starts_new_section(seg, text):
                    flush_text_chunk()
                    current_section_index += 1
                pending_empty_segment_ids.append(seg_id)
                continue

            if (
                seg_type == "table"
                and self.merge_small_tables_threshold_tokens is not None
                and table_has_trusted_header
                and seg_tokens <= int(self.merge_small_tables_threshold_tokens)
            ):
                extra = seg.get("extra") or {}
                caption = (
                    str(extra.get("caption") or "").strip()
                    if isinstance(extra, dict)
                    else str(getattr(extra, "caption", "") or "").strip()
                )
                if caption and caption not in text:
                    text = self._concat_prefix(f"{caption}\n\n", text)
                    seg_tokens = len(encode(text))

            if asset_modality == "video" and seg_type == "video_clip":
                flush_text_chunk()
                segment_ids = take_pending_empty_segment_ids() + [seg_id]
                results.append(
                    self._make_chunk(
                        content=text,
                        tokens=seg_tokens,
                        chunk_order_index=chunk_index,
                        file_path=doc_id,
                        doc_id=doc_id,
                        segment_ids=segment_ids,
                        order=chunk_index,
                        metadata=self._build_chunk_metadata(
                            asset=asset,
                            seg_map=seg_map,
                            segment_ids=segment_ids,
                        ),
                    )
                )
                chunk_index += 1
                continue

            # 以下类别的数据作为文本处理：文本文档，视频片段，小表格
            if treat_as_text(seg_type, seg_tokens, table_has_trusted_header):
                if (
                    self.respect_semantic_boundaries
                    and buffer_text_parts
                    and starts_new_section(seg, text)
                ):
                    flush_text_chunk()
                    current_section_index += 1
                elif self.respect_semantic_boundaries and starts_new_section(seg, text):
                    current_section_index += 1
                is_short_section_marker = (
                    starts_new_section(seg, text) and seg_tokens < text_max_token_size
                )
                if pending_empty_segment_ids:
                    buffer_segment_ids.extend(take_pending_empty_segment_ids())
                # 使用语义分割替代简单的 token 限制切分
                parts = (
                    self._semantic_split_text(
                        tokenizer, text, text_max_token_size, overlap_token_size
                    )
                    if seg_tokens > text_max_token_size
                    else [text]
                )
                for part in parts:
                    if buffer_starts_with_short_section_marker:
                        available_tokens = text_max_token_size - buffer_token_count
                        body_parts = self._semantic_split_text(
                            tokenizer, part, available_tokens, overlap_token_size
                        )
                        if body_parts:
                            append_text_part(body_parts[0], seg_id)
                            for body_part in body_parts[1:]:
                                append_text_part(body_part, seg_id)
                            continue
                    append_text_part(part, seg_id)
                if is_short_section_marker:
                    buffer_starts_with_short_section_marker = True
                continue

            # 模态边界触发：先输出已缓冲文本，尽量保持阅读顺序。
            if (
                seg_type in {"image", "table"}
                and self.enable_modal_boundary_flush
                and buffer_text_parts
            ):
                flush_text_chunk(modal_boundary=True)

            segment_ids = take_pending_empty_segment_ids() + [seg_id]

            # 图像保持原子性；表格即使共享 image metadata 类型也必须受 token 预算约束。
            if seg_type == "image":
                extra = seg.get("extra") or {}
                caption = (
                    str(extra.get("caption") or "").strip()
                    if isinstance(extra, dict)
                    else str(getattr(extra, "caption", "") or "").strip()
                )
                content = (
                    text if not caption or text.startswith(caption) else f"{caption}\n\n{text}"
                )
                results.append(
                    self._make_chunk(
                        content=content,
                        tokens=len(encode(content)),
                        chunk_order_index=chunk_index,
                        file_path=doc_id,
                        doc_id=doc_id,
                        segment_ids=segment_ids,
                        order=chunk_index,
                        metadata=self._build_chunk_metadata(
                            asset=asset,
                            seg_map=seg_map,
                            segment_ids=segment_ids,
                        ),
                    )
                )
                chunk_index += 1
                continue

            # 模态：可选插入标记
            if self.insert_modal_markers_in_text:
                marker = (
                    "[图像]"
                    if seg_type == "image"
                    else ("[表格]" if seg_type == "table" else f"[{seg_type}]")
                )
                marker_tokens = len(encode(marker))
                ensure_space_for(marker_tokens)
                buffer_text_parts.append(marker)
                buffer_token_count += marker_tokens

            # 对模态内容也使用语义分割（如表格的描述文本）
            modal_parts: List[str]
            # 对图/表段，严格使用 parser 写入的 seg.extra.caption 作为“额外上下文”，
            # 并将其保留到该段被切分后的每一个子块开头（避免依赖“[xxx]...”格式的启发式判断）。
            if seg_type in {"image", "table"}:
                extra = seg.get("extra") or {}
                if isinstance(extra, dict):
                    caption_prefix = str(extra.get("caption") or "").strip()
                    seg_source = str(extra.get("source") or "").strip().lower()
                else:
                    caption_prefix = str(getattr(extra, "caption", "") or "").strip()
                    seg_source = ""

                # For visual-PPT flow, keep raw text splitting behavior and avoid
                # replaying caption prefix to every modal split chunk.
                preserve_caption_prefix = seg_source != "ppt_visual"

                if caption_prefix and preserve_caption_prefix:
                    prefix = caption_prefix.rstrip() + "\n\n"
                    body = text
                    if text.startswith(caption_prefix):
                        body = text[len(caption_prefix) :].lstrip()

                    prefix_tokens = len(encode(prefix))
                    # 若前缀过长，无法保证每块都携带它且不超限：回退普通切分
                    if prefix_tokens >= max_token_size:
                        modal_parts = (
                            self._semantic_split_text(
                                tokenizer, text, max_token_size, overlap_token_size
                            )
                            if seg_tokens > max_token_size
                            else [text]
                        )
                    else:
                        effective_max_tokens = max(1, max_token_size - prefix_tokens)
                        effective_overlap = min(
                            max(0, overlap_token_size), max(0, effective_max_tokens - 1)
                        )
                        body_tokens = len(encode(body))
                        body_parts = (
                            self._semantic_split_text(
                                tokenizer,
                                body,
                                effective_max_tokens,
                                effective_overlap,
                            )
                            if body_tokens > effective_max_tokens
                            else [body]
                        )
                        modal_parts = [
                            self._concat_prefix(prefix, part) for part in body_parts if part.strip()
                        ]
                        if not modal_parts:
                            modal_parts = [caption_prefix]
                else:
                    modal_parts = (
                        self._semantic_split_text(
                            tokenizer, text, max_token_size, overlap_token_size
                        )
                        if seg_tokens > max_token_size
                        else [text]
                    )
            else:
                modal_parts = (
                    self._semantic_split_text(tokenizer, text, max_token_size, overlap_token_size)
                    if seg_tokens > max_token_size
                    else [text]
                )
            for part in modal_parts:
                tokens_val = len(encode(part))
                results.append(
                    self._make_chunk(
                        content=part,
                        tokens=tokens_val,
                        chunk_order_index=chunk_index,
                        file_path=doc_id,
                        doc_id=doc_id,
                        segment_ids=segment_ids,
                        order=chunk_index,
                        metadata=self._build_chunk_metadata(
                            asset=asset,
                            seg_map=seg_map,
                            segment_ids=segment_ids,
                        ),
                    )
                )
                chunk_index += 1

        flush_text_chunk()
        if pending_empty_segment_ids:
            if results:
                last_chunk = results[-1]
                segment_ids = list(
                    dict.fromkeys(last_chunk["segment_ids"] + pending_empty_segment_ids)
                )
                last_chunk["segment_ids"] = segment_ids
                last_chunk["metadata"] = self._build_chunk_metadata(
                    asset=asset,
                    seg_map=seg_map,
                    segment_ids=segment_ids,
                    edited=bool((last_chunk.get("metadata") or {}).get("edited")),
                )
            else:
                results.append(
                    self._make_chunk(
                        content="",
                        tokens=0,
                        chunk_order_index=chunk_index,
                        file_path=doc_id,
                        doc_id=doc_id,
                        segment_ids=pending_empty_segment_ids,
                        order=chunk_index,
                        metadata=self._build_chunk_metadata(
                            asset=asset,
                            seg_map=seg_map,
                            segment_ids=pending_empty_segment_ids,
                        ),
                    )
                )
        return results

    @staticmethod
    def _concat_prefix(prefix: str, body: str) -> str:
        if not prefix:
            return body
        return prefix + (body.lstrip("\n") if body else "")

    @staticmethod
    def _table_content(seg: dict[str, Any], fallback: str) -> tuple[str, bool]:
        """Return recoverable table content and whether it has an explicit header."""
        extra = seg.get("extra") or {}
        table_body = str(extra.get("table_body") or "") if isinstance(extra, dict) else ""
        if not table_body:
            return fallback, True

        class _TableRows(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.rows: list[list[str]] = []
                self.current_row: list[str] = []
                self.current_cell: list[str] = []
                self.in_cell = False
                self.first_row_has_header = False

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "tr":
                    self.current_row = []
                elif tag in {"td", "th"}:
                    self.in_cell = True
                    self.current_cell = []
                    if tag == "th" and not self.rows:
                        self.first_row_has_header = True

            def handle_data(self, data: str) -> None:
                if self.in_cell:
                    self.current_cell.append(data)

            def handle_endtag(self, tag: str) -> None:
                if tag in {"td", "th"} and self.in_cell:
                    cell = re.sub(r"\s+", " ", "".join(self.current_cell)).strip()
                    self.current_row.append(cell)
                    self.current_cell = []
                    self.in_cell = False
                elif tag == "tr" and self.current_row:
                    self.rows.append(self.current_row)
                    self.current_row = []

        parser = _TableRows()
        try:
            parser.feed(table_body)
            parser.close()
        except Exception:
            return fallback, False
        if not parser.rows:
            return fallback, False

        def row_text(row: list[str]) -> str:
            return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"

        rows = [row_text(row) for row in parser.rows]
        table_text = (
            "\n".join([rows[0], "| " + " | ".join("---" for _ in parser.rows[0]) + " |", *rows[1:]])
            if parser.first_row_has_header
            else "Table rows:\n"
            + "\n".join(
                "- " + " | ".join(cell.replace("|", "\\|") for cell in row) for row in parser.rows
            )
        )
        caption = str(extra.get("caption") or "").strip() if isinstance(extra, dict) else ""
        return (
            f"{caption}\n\n{table_text}" if caption else table_text,
            parser.first_row_has_header,
        )

    def _semantic_split_text(
        self,
        tokenizer: Tokenizer,
        text: str,
        max_tokens: int,
        overlap_tokens: int = 0,
        _recursion_depth: int = 0,
    ) -> List[str]:
        """
        使用语义感知的方式切分文本，保持语义完整性。

        策略：
        1. 如果启用语义分割且文本长度合适，使用 RecursiveCharacterTextSplitter
        2. 优先在段落、句子等语义边界处切分
        3. 确保每个 chunk 不超过 token 限制

        Args:
            tokenizer: 分块 tokenizer
            text: 待切分的文本
            max_tokens: 最大 token 数
            overlap_tokens: 重叠 token 数
            _recursion_depth: 递归深度（内部使用）

        Returns:
            切分后的文本片段列表
        """
        if not text or not text.strip():
            return []

        # 防止无限递归：超过5层深度时回退到简单切分
        if _recursion_depth >= 5:
            logger.warning(
                f"Maximum recursion depth reached ({_recursion_depth}), "
                f"falling back to simple splitting"
            )
            return self._split_text_by_token_limit(tokenizer, text, max_tokens)

        # 计算文本的 token 数
        tokens = self._safe_encode(tokenizer, text)
        if len(tokens) <= max_tokens:
            return [text]

        # Markdown 表格保护：当表格过长需要切分时，按“行”拆分并为每个子块重复表头，
        # 避免出现“无表头的表格尾巴”导致检索难命中。
        table_aware = self._split_text_preserving_markdown_tables(
            tokenizer=tokenizer,
            text=text,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            _recursion_depth=_recursion_depth,
        )
        if table_aware is not None:
            return table_aware

        # 如果未启用语义分割，回退到简单切分
        if not self.enable_semantic_splitting or self.text_splitter is None:
            return self._split_text_by_token_limit(tokenizer, text, max_tokens)

        try:
            # 估算字符到 token 的比例
            char_to_token_ratio = len(text) / max(1, len(tokens))

            # 根据递归深度调整安全系数，越深层递归，越保守
            safety_factor = 0.8 / (1 + _recursion_depth * 0.2)  # 0.8 -> 0.67 -> 0.57...

            # 根据 token 限制计算字符数限制
            estimated_chunk_size = int(max_tokens * char_to_token_ratio * safety_factor)
            estimated_overlap_size = int(overlap_tokens * char_to_token_ratio * safety_factor)

            # 确保 chunk_size 有意义（至少10个字符）
            if estimated_chunk_size < 10:
                logger.warning(
                    f"Estimated chunk size too small ({estimated_chunk_size}), "
                    f"falling back to simple splitting"
                )
                return self._split_text_by_token_limit(tokenizer, text, max_tokens)

            # 更新 text_splitter 的参数
            self.text_splitter._chunk_size = estimated_chunk_size
            self.text_splitter._chunk_overlap = max(0, estimated_overlap_size)

            # 使用 RecursiveCharacterTextSplitter 进行语义切分
            chunks = self.text_splitter.split_text(text)

            # 如果切分没有效果（还是一个大块），回退
            if len(chunks) <= 1 and len(tokens) > max_tokens:
                return self._split_text_by_token_limit(tokenizer, text, max_tokens)

            # 验证并调整：确保每个 chunk 不超过 token 限制
            validated_chunks: List[str] = []
            for chunk in chunks:
                chunk_tokens = self._safe_encode(tokenizer, chunk)
                if len(chunk_tokens) <= max_tokens:
                    validated_chunks.append(chunk)
                else:
                    # 如果某个 chunk 仍然超过限制，递归切分
                    logger.debug(
                        f"Semantic chunk exceeded token limit ({len(chunk_tokens)} > {max_tokens}), "
                        f"performing recursive split (depth={_recursion_depth + 1})"
                    )
                    sub_chunks = self._semantic_split_text(
                        tokenizer, chunk, max_tokens, overlap_tokens, _recursion_depth + 1
                    )
                    validated_chunks.extend(sub_chunks)

            return validated_chunks if validated_chunks else [text]

        except Exception as e:
            logger.warning(f"Semantic splitting failed: {e}, falling back to simple splitting")
            return self._split_text_by_token_limit(tokenizer, text, max_tokens)

    def _split_text_preserving_markdown_tables(
        self,
        *,
        tokenizer: Tokenizer,
        text: str,
        max_tokens: int,
        overlap_tokens: int,
        _recursion_depth: int,
    ) -> Optional[List[str]]:
        """将包含 Markdown 表格的长文本切分为多个片段。

        规则：
        - 非表格部分照常语义切分/按 token 切分
        - 表格块按行切分，并在每个子块前重复表头（header + separator）

        返回：
        - 若检测到 Markdown 表格并完成切分，返回切分结果（List[str]）
        - 若未检测到表格，返回 None（交由普通语义切分逻辑处理）
        """
        if "|" not in text:
            return None

        blocks = self._split_into_markdown_table_blocks(text)
        if not any(kind == "table" for kind, _ in blocks):
            return None

        def encode(s: str) -> List[int]:
            return self._safe_encode(tokenizer, s)

        pieces: List[str] = []
        for kind, block in blocks:
            if not block or not block.strip():
                continue

            if kind == "table":
                table_pieces = self._split_markdown_table_block(
                    tokenizer=tokenizer,
                    table_block=block,
                    max_tokens=max_tokens,
                )
                if table_pieces is None:
                    # 识别失败则回退普通切分
                    if len(encode(block)) <= max_tokens:
                        pieces.append(block)
                    else:
                        pieces.extend(
                            self._semantic_split_text(
                                tokenizer,
                                block,
                                max_tokens,
                                overlap_tokens,
                                _recursion_depth + 1,
                            )
                        )
                else:
                    pieces.extend(table_pieces)
                continue

            # 非表格块
            if len(encode(block)) <= max_tokens:
                pieces.append(block)
            else:
                pieces.extend(
                    self._semantic_split_text(
                        tokenizer,
                        block,
                        max_tokens,
                        overlap_tokens,
                        _recursion_depth + 1,
                    )
                )

        return pieces

    @staticmethod
    def _looks_like_md_table_row(line: str) -> bool:
        # 允许首尾管道可选；要求至少出现一个“|”分隔
        s = (line or "").strip()
        return bool(s) and "|" in s and not s.startswith("```")

    @staticmethod
    def _looks_like_md_table_separator(line: str) -> bool:
        # 典型分隔行：| --- | --- | 或 |:---|---:|
        s = (line or "").strip()
        if not s or "|" not in s:
            return False
        # 去掉首尾管道后，检查每一列是否类似 --- / :--- / ---: / :---:
        core = s.strip("|")
        cols = [c.strip() for c in core.split("|")]
        if not cols:
            return False
        pattern = re.compile(r"^:?-{3,}:?$")
        return all(bool(pattern.match(c)) for c in cols)

    def _split_into_markdown_table_blocks(self, text: str) -> List[tuple[str, str]]:
        """将文本切成 (kind, block) 列表，其中 kind in {"text", "table"}。

        通过识别 Markdown 表格 header 行 + separator 行来确定表格块。
        """
        lines = text.splitlines()
        blocks: List[tuple[str, str]] = []
        i = 0

        def flush_text(buf: List[str]) -> None:
            if buf and "".join(buf).strip():
                blocks.append(("text", "\n".join(buf)))
            buf.clear()

        text_buf: List[str] = []
        while i < len(lines):
            line = lines[i]
            next_line = lines[i + 1] if i + 1 < len(lines) else ""

            is_table_start = self._looks_like_md_table_row(
                line
            ) and self._looks_like_md_table_separator(next_line)

            if not is_table_start:
                text_buf.append(line)
                i += 1
                continue

            # 开始一个表格块
            flush_text(text_buf)
            table_lines = [line, next_line]
            i += 2
            while i < len(lines):
                cur = lines[i]
                if not self._looks_like_md_table_row(cur):
                    break
                # 遇到空行或非表格行则结束
                if not cur.strip():
                    break
                table_lines.append(cur)
                i += 1
            blocks.append(("table", "\n".join(table_lines)))

        flush_text(text_buf)
        return blocks

    def _split_markdown_table_block(
        self,
        *,
        tokenizer: Tokenizer,
        table_block: str,
        max_tokens: int,
    ) -> Optional[List[str]]:
        """将单个 Markdown 表格按行切分，确保每段都有表头。

        返回 None 表示表格结构不符合预期（回退普通切分）。
        """
        if not table_block or "|" not in table_block:
            return None

        lines = [ln.rstrip("\n") for ln in table_block.splitlines() if ln is not None]
        if len(lines) < 2:
            return None
        if not (
            self._looks_like_md_table_row(lines[0])
            and self._looks_like_md_table_separator(lines[1])
        ):
            return None

        def encode(s: str) -> List[int]:
            return self._safe_encode(tokenizer, s)

        header = [lines[0], lines[1]]
        header_text = "\n".join(header)
        header_tokens = len(encode(header_text))
        if header_tokens >= max_tokens:
            # 表头本身就超限：无法可靠按行拆分，回退
            return None

        data_rows = lines[2:]
        if not data_rows:
            return ["\n".join(lines)]

        pieces: List[str] = []
        current_rows: List[str] = []

        def emit(rows: List[str]) -> None:
            if not rows:
                return
            pieces.append("\n".join(header + rows))

        for row in data_rows:
            # 忽略完全空的行（通常意味着表格结束）
            if not row.strip():
                continue

            candidate_rows = current_rows + [row]
            candidate_text = "\n".join(header + candidate_rows)
            if len(encode(candidate_text)) <= max_tokens:
                current_rows = candidate_rows
                continue

            # 当前块已满，先输出
            emit(current_rows)
            current_rows = []

            # 该行单独也放不下：把这一行再按 token 近似切分，每段仍带表头
            row_text = "\n".join(header + [row])
            if len(encode(row_text)) > max_tokens:
                available = max(1, max_tokens - header_tokens)
                row_parts = self._split_text_by_token_limit(tokenizer, row, available)
                for part in row_parts:
                    pieces.append("\n".join(header + [part]))
                continue

            current_rows = [row]

        emit(current_rows)
        return pieces if pieces else ["\n".join(lines)]

    @staticmethod
    def _split_text_by_token_limit(tokenizer: Tokenizer, text: str, max_tokens: int) -> List[str]:
        """Split text within the tokenizer budget, preferring semantic boundaries."""
        tokens = HyperChunker._safe_encode(tokenizer, text)
        if not tokens:
            return [text]
        if len(tokens) <= max_tokens:
            return [text]

        parts: List[str] = []
        start = 0
        while start < len(text):
            end = HyperChunker._max_prefix_within_token_budget(tokenizer, text, start, max_tokens)
            if end <= start:
                end = start + 1

            boundary = HyperChunker._last_semantic_boundary(text, start, end)
            if boundary is not None:
                end = boundary
            parts.append(text[start:end])
            start = end
        return parts

    @staticmethod
    def _max_prefix_within_token_budget(
        tokenizer: Tokenizer, text: str, start: int, max_tokens: int
    ) -> int:
        left = start + 1
        right = len(text)
        best_end = start
        while left <= right:
            middle = (left + right) // 2
            if len(HyperChunker._safe_encode(tokenizer, text[start:middle])) <= max_tokens:
                best_end = middle
                left = middle + 1
            else:
                right = middle - 1
        return best_end

    @staticmethod
    def _last_semantic_boundary(text: str, start: int, end: int) -> Optional[int]:
        for index in range(end - 1, start, -1):
            if text[index] in "\n。！？.!?；;":
                return index + 1
        return None

    @staticmethod
    def _safe_encode(tokenizer: Tokenizer, text: str) -> List[int]:
        return getattr(tokenizer, "encode", lambda x: [])(text)

    @classmethod
    def _runtime_token_count(cls, tokenizer: Tokenizer, text: str) -> int:
        """Return the sole budget measurement used by production chunking."""
        return len(cls._safe_encode(tokenizer, text))

    def _chunk_plain_text_by_token_size(
        self,
        tokenizer: Tokenizer,
        content: str,
        overlap_token_size: int,
        max_token_size: int,
    ) -> List[Dict[str, Any]]:
        token_ids = self._safe_encode(tokenizer, content or "")
        if token_ids:
            try:
                decode = getattr(tokenizer, "decode")
            except Exception:
                decode = None
            if decode is None:
                try:
                    encoding = tiktoken.encoding_for_model("gpt-4o-mini")
                except Exception:
                    encoding = tiktoken.get_encoding("cl100k_base")
                decode = encoding.decode
        else:
            token_ids = []
            decode = None

        if not token_ids or decode is None:
            return [
                {
                    "content": content or "",
                    "tokens": len(token_ids),
                    "chunk_order_index": 0,
                }
            ]

        if len(token_ids) <= max_token_size:
            return [
                {
                    "content": content or "",
                    "tokens": len(token_ids),
                    "chunk_order_index": 0,
                }
            ]

        chunks: List[Dict[str, Any]] = []
        start = 0
        chunk_order_index = 0
        while start < len(token_ids):
            end = min(len(token_ids), start + max_token_size)
            piece_tokens = token_ids[start:end]
            chunks.append(
                {
                    "content": decode(piece_tokens),
                    "tokens": len(piece_tokens),
                    "chunk_order_index": chunk_order_index,
                }
            )
            chunk_order_index += 1
            if end >= len(token_ids):
                break
            start = max(0, end - max(0, overlap_token_size))
        return chunks

    async def _build_document_context(self, segments: List[Segment]) -> Optional[str]:
        """
        从 segments 中构建文档级别的上下文描述。
        使用 LLM 对文档全文进行 2-3 句话的总结，作为文档主旨上下文。

        Args:
            segments: 文档的 segments 列表(文档解析器切分好的文档内容段)

        Returns:
            文档的摘要总结，用于生成 chunk 的上下文增强描述
        """
        if not segments:
            return None

        # 如果没有启用 contextual embedder 或没有 LLM 函数，返回 None
        if not self.contextual_embedder or not self.contextual_embedder.llm_func:
            return None

        try:
            # 拼接所有 segment 的文本内容
            full_content = "\n".join(
                segment.get("text", "").strip()
                for segment in segments
                if segment.get("text", "").strip()
            )
            # 限制输入长度，避免过长，LLM输入限制 64k token的情况下，汉字长度约为100k字符
            if len(full_content) > 50000:
                full_content = full_content[:50000]
            if not full_content:
                return None

            # 构建总结 prompt
            summary_prompt = self._build_document_summary_prompt(full_content)

            # 调用 LLM 生成摘要
            import asyncio

            result = self.contextual_embedder.llm_func(summary_prompt)

            # 处理异步/同步 LLM 函数
            if asyncio.iscoroutine(result):
                summary = await result
            else:
                summary = result

            if summary and isinstance(summary, str):
                # 清理摘要文本
                summary = " ".join(summary.strip().split())
                logger.info("HyperChunker: successfully generated document summary.")
                return summary
            else:
                logger.warning("HyperChunker: failed to generate document summary")
                return None

        except Exception as e:
            logger.error(f"HyperChunker: error generating document summary: {e}")
            return None

    def _build_document_summary_prompt(self, document_content: str) -> str:
        """
        构建文档总结的 LLM prompt。

        Args:
            document_content: 文档的完整内容

        Returns:
            用于生成文档摘要的 prompt
        """
        prompt_parts = [
            "请为以下文档生成一个简短的摘要（2-3句话），概括文档的主要内容、主题和目的。",
            "这个摘要将用于帮助理解文档中各个片段的上下文背景。\n",
            f"<文档内容>\n{document_content}\n</文档内容>\n",
            "请直接输出摘要内容，不要包含任何前缀（如'摘要：'、'总结：'等）。",
            "摘要应该简洁明了，不超过80个字。",
        ]

        return "\n".join(prompt_parts)

    def _make_chunk(
        self,
        *,
        content: Optional[str] = None,
        tokens: Optional[int] = None,
        chunk_order_index: Optional[int] = None,
        file_path: Optional[str] = None,
        doc_id: Optional[str] = None,
        segment_ids: Optional[List[str]] = None,
        order: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """统一构造/标准化 chunk：
        - 可直接 _make_chunk(**ch)
        - 也可用我们内部计算的 content/tokens 等调用
        仅输出必要字段：content, tokens, chunk_order_index, file_path, doc_id, segment_ids, order。
        """
        assert content is not None
        assert chunk_order_index is not None
        if tokens is None:
            # 无 tokenizer 参与时，尽量要求外部传入 tokens
            tokens = len((content or "").split())
        if order is None:
            order = int(chunk_order_index)
        if file_path is None:
            file_path = doc_id or ""
        if segment_ids is None:
            segment_ids = []
        # 在 content 顶部加入一行 JSON 标记，携带 doc_id 与 order（不再包含 segment_ids）
        try:
            content_with_header = {
                "doc_id": doc_id or "",
                "order": int(order),
                "content": content or "",
            }
            content_with_header = json.dumps(content_with_header, ensure_ascii=False)
        except Exception:
            # 兜底：若序列化异常，使用最小安全标记
            content_with_header = json.dumps({"content": content or ""}, ensure_ascii=False)
        out: Dict[str, Any] = {
            "content": content_with_header,
            "tokens": int(tokens),
            "chunk_order_index": int(chunk_order_index),
            "file_path": file_path,
            "doc_id": doc_id or "",
            "segment_ids": list(segment_ids),
            "order": int(order),
        }

        if metadata:
            out["metadata"] = metadata

        return out

    def _build_chunk_metadata(
        self,
        *,
        asset: Dict[str, Any],
        seg_map: Dict[str, Dict[str, Any]],
        segment_ids: List[str],
        edited: bool = False,
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        unique_segment_ids = list(dict.fromkeys(segment_ids or []))
        metadata["edited"] = bool(edited)

        locations = self._build_locations_from_segments(
            asset=asset,
            seg_map=seg_map,
            segment_ids=unique_segment_ids,
        )
        if locations:
            metadata["locations"] = locations

        asset_modality = str((asset or {}).get("modality") or "").lower()
        if asset_modality == "video":
            metadata["chunk_type"] = "video"
            asset_id = str((asset or {}).get("doc_id") or "")
            asset_extra = (asset or {}).get("extra") or {}
            metadata["asset_id"] = asset_id
            metadata["video_id"] = str(
                asset_extra.get("video_id") or asset_extra.get("source_id") or asset_id
            )
            metadata["source_id"] = str(
                asset_extra.get("source_id") or asset_extra.get("video_id") or asset_id
            )
            metadata["source_parser"] = str(
                asset_extra.get("source_parser") or (asset or {}).get("doc_type") or "video"
            )
            global_metadata = asset_extra.get("global_metadata")
            if global_metadata:
                metadata["global_metadata"] = str(global_metadata)
            begin_ts: Optional[float] = None
            end_ts: Optional[float] = None
            video_uuids: List[str] = []
            for sid in unique_segment_ids:
                seg = seg_map.get(str(sid)) or {}
                loc = seg.get("location") or {}
                b = loc.get("begin_ts")
                e = loc.get("end_ts")
                if b is not None:
                    try:
                        b_val = float(b)
                        begin_ts = b_val if begin_ts is None else min(begin_ts, b_val)
                    except Exception:
                        pass
                if e is not None:
                    try:
                        e_val = float(e)
                        end_ts = e_val if end_ts is None else max(end_ts, e_val)
                    except Exception:
                        pass
                video_uuids.append(f"viknow-{asset_id}-{sid}")
            if begin_ts is not None:
                metadata["begin_ts"] = begin_ts
            if end_ts is not None:
                metadata["end_ts"] = end_ts
            metadata["video_uuids"] = video_uuids
            display_metadata = [
                build_video_chunk_display_metadata(seg_map.get(str(sid), {}).get("main_struct"))
                for sid in unique_segment_ids
            ]
            # Keep claims attached to the segment-level chunk; context/title only
            # improve retrieval text and must never rewrite these factual records.
            metadata["claims_by_segment"] = [
                {
                    "segment_id": str(sid),
                    "schema_version": (seg_map.get(str(sid), {}).get("main_struct") or {}).get(
                        "claims_schema_version"
                    ),
                    "claims": (seg_map.get(str(sid), {}).get("main_struct") or {}).get("claims")
                    or [],
                }
                for sid in unique_segment_ids
            ]
            for key in ("caption", "object", "ocr", "asr"):
                values = [
                    value
                    for item in display_metadata
                    if (value := str(item.get(key) or "").strip())
                ]
                metadata[key] = "\n".join(dict.fromkeys(values))
            return metadata

        has_image = False
        image_url = None
        for sid in unique_segment_ids:
            seg = seg_map.get(str(sid)) or {}
            seg_type = str(seg.get("type", "text"))
            if seg_type in ("image", "table"):
                has_image = True
                if image_url is None:
                    extra = seg.get("extra") or {}
                    if isinstance(extra, dict):
                        image_url = (
                            extra.get("image_path")
                            or extra.get("image_ref")
                            or extra.get("img_path")
                        )
        metadata["chunk_type"] = "image" if has_image else "text"
        if image_url:
            metadata["image_url"] = image_url
        return metadata

    @staticmethod
    def _build_locations_from_segments(
        *,
        asset: Dict[str, Any],
        seg_map: Dict[str, Dict[str, Any]],
        segment_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Build locations list compatible with chunk.metadata['locations'].

        Includes base segment location, and for image/table also includes
        caption_location/footnote_location stored in segment.extra.
        """
        locations: List[Dict[str, Any]] = []

        page_info = asset.get("page_info", {}) if isinstance(asset, dict) else {}
        total_pages = page_info.get("total_pages", 0)
        page_sizes = page_info.get("page_sizes", {})

        def add_loc(
            *,
            sid: str,
            page: Any,
            bbox: Any,
            row: Any = None,
            col_start: Any = None,
            col_end: Any = None,
            col_len: Any = None,
            parent_sid: Optional[str] = None,
            role: Optional[str] = None,
            text: Optional[str] = None,
        ) -> None:
            if page is None:
                return
            has_bbox = bool(bbox)
            has_row = row is not None
            if not has_bbox and not has_row:
                return
            entry: Dict[str, Any] = {"segment_id": sid, "page": page, "bbox": bbox}
            if not has_bbox:
                entry.pop("bbox", None)
            if has_row:
                entry["row"] = row
            if col_start is not None:
                entry["col_start"] = col_start
            if col_end is not None:
                entry["col_end"] = col_end
            if col_len is not None:
                entry["col_len"] = col_len
            if parent_sid:
                entry["parent_segment_id"] = parent_sid
            if role:
                entry["role"] = role
            if text:
                entry["text"] = str(text)
            entry["total_pages"] = total_pages
            entry["page_size"] = page_sizes.get(str(page))
            locations.append(entry)

        # Keep stable order but avoid duplicates.
        unique_segment_ids = list(dict.fromkeys(segment_ids or []))

        for sid in unique_segment_ids:
            seg = seg_map.get(sid) or {}
            loc = seg.get("location") or {}
            seg_text = seg.get("text")
            add_loc(
                sid=sid,
                page=loc.get("page"),
                bbox=loc.get("bbox"),
                row=loc.get("row"),
                col_start=loc.get("col_start"),
                col_end=loc.get("col_end"),
                col_len=loc.get("col_len"),
                text=seg_text,
            )

            seg_type = str(seg.get("type", "text"))
            if seg_type in ("image", "table"):
                extra = seg.get("extra") or {}
                for role, key in (
                    ("caption", "caption_location"),
                    ("footnote", "footnote_location"),
                ):
                    loc_extra = extra.get(key)
                    if not loc_extra:
                        continue
                    loc_list = loc_extra if isinstance(loc_extra, list) else [loc_extra]
                    for le in loc_list:
                        if not isinstance(le, dict):
                            continue
                        role_text = None
                        if role == "caption":
                            role_text = extra.get("caption")
                        elif role == "footnote":
                            role_text = extra.get("footnote")
                        add_loc(
                            sid=f"{sid}::{role}",
                            parent_sid=sid,
                            role=role,
                            page=le.get("page"),
                            bbox=le.get("bbox"),
                            text=role_text,
                        )

        return locations
