"""
MinerU Parser for HyperRAG

This module provides parsers for converting MinerU output into HyperRAG's
three-layer intermediate representation (Asset -> Segments -> Chunks).

MinerU outputs include:
- content_list.json: Structured content with type, text, and page information
- middle.json: Detailed layout information with bounding boxes
- images/: Extracted images referenced in content_list.json

Key Technical Approach:
The parser uses direct indexing between content_list.json and middle.json
para_blocks based on their one-to-one correspondence within each page. This
ensures 100% bbox coverage by leveraging the inherent structural relationship
rather than content matching algorithms.
"""

import hashlib
import io
import json
import logging
import os
import platform
import posixpath
import re
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from openai import OpenAI

from .config_loader import HyperRAGConfig
from .schema import Asset, Segment

logger = logging.getLogger(__name__)


def mineru_timeout_seconds() -> float:
    """Resolve mineru subprocess timeout. Always positive so parse cannot hang forever."""
    raw = os.environ.get("HYPERRAG_MINERU_TIMEOUT_SECONDS")
    if raw is not None and raw.strip():
        timeout = float(raw)
    else:
        timeout = float(HyperRAGConfig.mineru_timeout_seconds)
    if timeout <= 0:
        raise ValueError("mineru timeout must be > 0 seconds")
    return timeout


def _kill_mineru_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if platform.system() != "Windows" and proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
    else:
        proc.kill()
    proc.wait()


class MinerUParser:
    """
    Unified MinerU parser that handles both command-line execution and output processing.

    This class combines MinerU command-line tool invocation with result parsing,
    providing a complete solution for document processing with caching capabilities.

    MinerU generates structured document analysis with:
    - content_list.json: Sequential content items (text, images, tables, equations)
    - middle.json: Detailed layout and positioning data (optional)
    - images/: Directory containing extracted images
    """

    # Define common file formats (inherited from Parser base class)
    OFFICE_FORMATS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
    EXCEL_FORMATS = {".xls", ".xlsx"}
    IMAGE_FORMATS = {".png", ".jpeg", ".jpg", ".bmp", ".tiff", ".tif", ".gif", ".webp"}
    TEXT_FORMATS = {".txt", ".md"}
    PPT_FORMATS = {".ppt", ".pptx"}
    PPT_EMBEDDED_AUDIO_FORMATS = {
        ".aac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".wav",
        ".wma",
        ".flac",
        ".opus",
    }
    PPT_EMBEDDED_VIDEO_FORMATS = {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".wmv",
        ".webm",
        ".mpeg",
        ".mpg",
    }

    # Class-level lock dictionary for file-level synchronization
    _file_locks = {}
    _locks_lock = threading.Lock()

    # Default max distance (in MinerU bbox coordinate space) for caption recovery.
    DEFAULT_MAX_CAPTION_DISTANCE = 30.0

    # Candidate window size (by content_list order) for caption recovery.
    DEFAULT_CAPTION_CANDIDATE_WINDOW = 12

    # Scoring weights for caption recovery (soft preferences; tune as needed).
    CAPTION_ALIGN_WEIGHT = 1.5
    CAPTION_OVERLAP_DISTANCE_SCALE = 50.0
    CAPTION_SIZE_WEIGHT = 0.25
    CAPTION_NEWLINE_WEIGHT = 1.5
    CAPTION_LENGTH_WEIGHT = 1.0  # 字数超过100候选caption文本的惩罚权重

    # Common slide aspect ratios for presentation-style PDFs.
    PPT_LANDSCAPE_RATIOS = (4 / 3, 16 / 10, 16 / 9)

    # Fallback order for plain-text documents before handing them to LibreOffice.
    TEXT_READ_ENCODINGS = (
        "utf-8",
        "utf-8-sig",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "gb18030",
        "gbk",
        "big5",
        "cp1252",
        "latin-1",
    )

    def __init__(self, asr_model_func: Optional[Dict[str, Any]] = None):
        """
        Initialize MinerU parser.
        """
        logger.info("MinerUParser initialized")
        # Debug toggle: when enabled, store top candidate score breakdowns in segment.extra
        self.debug_caption_recovery: bool = False
        self.asr_model_func: Dict[str, Any] = dict(asr_model_func or {})

    @staticmethod
    def _resolve_mineru_mid_path(backend: Optional[str] = None) -> str:
        resolved_backend = str(backend or HyperRAGConfig.mineru_backend or "").strip().lower()
        if resolved_backend == "pipeline":
            return "auto"
        if resolved_backend == "vlm-http-client":
            return "vlm"
        return HyperRAGConfig.mineru_mid_path

    def _resolve_whisper_client_kwargs(self, parser_params: Dict[str, Any]) -> Dict[str, str]:
        """Resolve Whisper client config with runtime override -> injected config -> defaults."""
        pp = dict(parser_params or {})
        base_url = (
            pp.get("asr_base_url")
            or pp.get("whisper_base_url")
            or self.asr_model_func.get("base_url")
            or HyperRAGConfig.whisper_base_url
        )
        api_key = (
            pp.get("asr_api_key")
            or pp.get("whisper_api_key")
            or self.asr_model_func.get("api_key")
            or HyperRAGConfig.whisper_api_key
        )
        return {"base_url": str(base_url), "api_key": str(api_key)}

    @staticmethod
    def _replace_doc_title_in_caption_text(caption_text: str, new_doc_title: str) -> str:
        """Replace the [doc_title]... portion inside our structured caption_text.

        Expected format:
        [doc_title]{doc_title}[para_title]{para_title}[caption]{caption}
        """
        if not caption_text:
            return caption_text
        tag = "[doc_title]"
        if tag not in caption_text:
            return caption_text

        start = caption_text.find(tag) + len(tag)
        end = caption_text.find("[para_title]", start)
        if end == -1:
            end = caption_text.find("[caption]", start)
        if end == -1:
            end = len(caption_text)
        return caption_text[:start] + (new_doc_title or "") + caption_text[end:]

    def _apply_override_title_to_caption_segments(
        self, segments: List[Segment], override_title: str
    ) -> None:
        """Override [doc_title] in caption field for image/table segments."""
        if not override_title:
            return
        for seg in segments:
            if seg.type not in ("image", "table"):
                continue
            old_caption = str(seg.extra.get("caption", "") or "")
            if not old_caption:
                continue
            new_caption = self._replace_doc_title_in_caption_text(old_caption, override_title)
            if new_caption == old_caption:
                continue
            seg.extra["caption"] = new_caption

            # Keep segment.text consistent (it typically begins with caption_text).
            try:
                if isinstance(seg.text, str) and seg.text.startswith(old_caption):
                    seg.text = new_caption + seg.text[len(old_caption) :]
            except Exception:
                # Best-effort only; don't break parsing.
                pass

    @staticmethod
    def _normalize_structured_caption_segments(segments: List[Segment]) -> None:
        """Replace parser-only caption tags with one readable, indexable caption."""
        pattern = re.compile(
            r"^\[doc_title\](?P<doc_title>.*?)"
            r"\[para_title\](?P<para_title>.*?)"
            r"\[caption\](?P<caption>.*)$",
            re.DOTALL,
        )
        for segment in segments:
            if segment.type not in {"image", "table"}:
                continue

            original_caption = str(segment.extra.get("caption") or "").strip()
            match = pattern.fullmatch(original_caption)
            if match is None:
                continue

            doc_title = match.group("doc_title").strip()
            para_title = match.group("para_title").strip()
            caption = match.group("caption").strip()
            caption_parts = []
            for value in (para_title, caption):
                if value and value not in caption_parts:
                    caption_parts.append(value)
            normalized_caption = "\n".join(caption_parts)
            original_text = str(segment.text or "").strip()
            suffix = original_text.removeprefix(original_caption).strip()
            text_parts = list(caption_parts)
            if suffix and suffix not in text_parts:
                text_parts.append(suffix)

            segment.text = "\n\n".join(text_parts)
            segment.extra["caption"] = normalized_caption
            segment.extra["structured_caption"] = original_caption
            segment.extra["doc_title"] = doc_title
            segment.extra["para_title"] = para_title
            segment.extra["caption_text"] = caption
            segment.extra["caption_normalized"] = True

    @staticmethod
    def _normalize_parser_noise_segments(segments: List[Segment]) -> List[Segment]:
        """Drop parser placeholders and repeated page furniture without changing source order."""
        header_footer_keys: Dict[str, set] = {}
        candidates: List[Tuple[Segment, str, str]] = []

        for segment in segments:
            original_type = str(segment.extra.get("original_type") or "").strip().lower()
            if original_type not in {"header", "footer"}:
                continue

            normalized_text = " ".join(str(segment.text or "").split()).casefold()
            if not normalized_text:
                continue
            key = re.sub(r"\d+", "#", normalized_text)
            page = str(segment.location.get("page", ""))
            header_footer_keys.setdefault(f"{original_type}:{key}", set()).add(page)
            candidates.append((segment, original_type, key))

        repeated_furniture = {key for key, pages in header_footer_keys.items() if len(pages) > 1}
        repeated_segments = {
            id(segment)
            for segment, original_type, key in candidates
            if f"{original_type}:{key}" in repeated_furniture
        }

        normalized_segments: List[Segment] = []
        for segment in segments:
            original_type = str(segment.extra.get("original_type") or "").strip().lower()
            normalized_text = " ".join(str(segment.text or "").split())
            if segment.type == "text" and normalized_text in {"", "[空白文本]"}:
                continue
            if original_type == "page_number" or id(segment) in repeated_segments:
                continue
            normalized_segments.append(segment)

        return normalized_segments

    @contextmanager
    def _get_file_lock(self, doc_output_dir: Path):
        """
        获取文件级别的锁，确保同一个输出目录只有一个线程在处理

        Args:
            doc_output_dir: MinerU输出目录路径
        """
        lock_key = str(doc_output_dir.absolute())

        # 获取或创建文件级别的锁
        with self._locks_lock:
            if lock_key not in self._file_locks:
                self._file_locks[lock_key] = threading.Lock()
            file_lock = self._file_locks[lock_key]

        # 使用文件锁
        try:
            logger.debug(f"Acquiring lock for directory: {lock_key}")
            file_lock.acquire()
            logger.debug(f"Lock acquired for directory: {lock_key}")
            yield
        finally:
            file_lock.release()
            logger.debug(f"Lock released for directory: {lock_key}")

            # 清理不再使用的锁（可选优化）
            with self._locks_lock:
                # 如果锁没有其他线程等待，可以清理
                if not file_lock.locked() and lock_key in self._file_locks:
                    try:
                        # 尝试再次获取锁来检查是否有其他线程等待
                        if file_lock.acquire(blocking=False):
                            file_lock.release()
                            del self._file_locks[lock_key]
                    except Exception:
                        # 忽略并发清理中的异常，以避免影响主流程
                        pass

    def parse_document(
        self,
        doc_id: str,
        doc_path: Union[str, Path],
        mineru_dir: Union[str, Path],
        parser_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Asset, List[Segment]]:
        """
        Parse document using MinerU with caching support.

        Args:
            doc_id: Unique document identifier
            doc_path: Path to the document file
            mineru_dir: Directory for MinerU output
            parser_params: Optional dict to pass MinerU options in batch. Keys supported:
                - method: Parsing method (auto, txt, ocr)
                - lang: Document language for OCR optimization
                - device: Inference device (cpu, cuda, mps)
                - backend: MinerU backend
                - formula: Enable/disable formula parsing (bool)
                - table: Enable/disable table parsing (bool)
                - asr_hotwords: Optional ASR hotwords for PPT embedded media
                - ppt_media_asr_enabled: Enable ASR for embedded PPT media (bool)
                - ppt_notes_export_for_mineru: Export notes pages when converting PPT to PDF in MinerU mode (bool)

        Returns:
            Tuple of (Asset, List[Segment])
        """
        doc_path = Path(doc_path)
        mineru_dir = Path(mineru_dir)

        if not doc_path.exists():
            raise FileNotFoundError(f"Document does not exist: {doc_path}")

        pp = dict(parser_params or {})
        ppt_backbone = self._normalize_ppt_backbone(pp.get("ppt_backbone"))
        use_visual_ppt, ppt_detection_reason = self._should_use_visual_ppt_backbone(
            doc_path, ppt_backbone
        )

        is_excel = doc_path.suffix.lower() in self.EXCEL_FORMATS

        # Directory policy:
        # - Excel: custom parser writes directly under mineru_cache/<doc_id>/vlm
        # - Visual PPT: custom parser writes directly under mineru_cache/<doc_id>/vlm
        # - Non-Excel: keep original MinerU behavior (mineru appends <file_stem>/vlm)
        doc_output_dir = mineru_dir / doc_id if (is_excel or use_visual_ppt) else mineru_dir
        doc_output_dir.mkdir(parents=True, exist_ok=True)

        # Use per-asset lock key to avoid accidental global lock contention.
        lock_dir = mineru_dir / doc_id

        # Use file-level locking to ensure thread safety
        # 这里使用 file_lock 是为了保证在多线程或多进程环境下，
        # 同一个文档目录（doc_output_dir）在解析过程中不会被多个线程/进程同时操作，
        # 避免出现并发写入、重复解析或中间文件损坏等问题，实现线程安全和数据一致性。
        with self._get_file_lock(lock_dir):
            try:
                # Optional debug flag for caption recovery
                debug_flag = pp.get("debug_caption_recovery", False)
                if isinstance(debug_flag, str):
                    debug_flag = debug_flag.strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "y",
                        "on",
                    )
                self.debug_caption_recovery = bool(debug_flag)

                method = pp.get("method", "auto")
                mid_path = self._resolve_mineru_mid_path(pp.get("backend"))
                ppt_notes_context: Dict[str, Any] = {
                    "augmented_pptx_path": None,
                    "slide_notes_map": {},
                    "transcribed_media_count": 0,
                    "transcribed_slide_count": 0,
                }
                # Check if already parsed (double-check inside lock)
                if self._is_already_parsed(doc_output_dir, doc_path.stem, mid_path):
                    logger.info(f"Document {doc_id} already parsed, loading existing results")
                    asset, segments = self._load_existing_results(
                        doc_output_dir, doc_path, doc_id, mid_path=mid_path
                    )
                    if pp.get("title"):
                        override_title = str(pp.get("title"))
                        asset.title = override_title
                        self._apply_override_title_to_caption_segments(segments, override_title)
                    if use_visual_ppt and "source_parser" not in asset.extra:
                        asset.extra["source_parser"] = "ppt_visual"
                        asset.extra["ppt_origin_detection"] = ppt_detection_reason
                    return asset, segments

                if doc_path.suffix.lower() == ".pptx":
                    try:
                        ppt_notes_context = self._prepare_pptx_media_transcript_notes(
                            doc_id=doc_id,
                            doc_path=doc_path,
                            mineru_dir=mineru_dir,
                            parser_params=pp,
                        )
                    except Exception as e:
                        logger.warning(
                            f"PPT media transcript preparation failed for {doc_path.name}: {e}"
                        )
                    if ppt_notes_context.get("slide_notes_map"):
                        pp["ppt_slide_notes_map"] = ppt_notes_context.get("slide_notes_map", {})

                # Excel branch: bypass MinerU PDF conversion to avoid wide-table truncation.
                if is_excel:
                    logger.info(f"Parsing Excel document {doc_id} with native parser")
                    asset, segments = self._parse_excel_document(
                        doc_id=doc_id,
                        doc_path=doc_path,
                        doc_output_dir=mineru_dir / doc_id,
                        parser_params=pp,
                    )
                    if pp.get("title"):
                        override_title = str(pp.get("title"))
                        asset.title = override_title
                    asset.extra["ppt_transcribed_media_count"] = int(
                        ppt_notes_context.get("transcribed_media_count", 0) or 0
                    )
                    asset.extra["ppt_transcribed_slide_count"] = int(
                        ppt_notes_context.get("transcribed_slide_count", 0) or 0
                    )
                    return asset, segments

                if use_visual_ppt:
                    logger.info(
                        f"Parsing document {doc_id} with visual PPT backbone ({ppt_detection_reason})"
                    )
                    asset, segments = self._parse_visual_ppt_document(
                        doc_id=doc_id,
                        doc_path=doc_path,
                        doc_output_dir=mineru_dir / doc_id,
                        parser_params=pp,
                        detection_reason=ppt_detection_reason,
                    )
                    if pp.get("title"):
                        override_title = str(pp.get("title"))
                        asset.title = override_title
                    return asset, segments

                if ppt_backbone == "visual":
                    logger.info(
                        f"Document {doc_id} requested visual PPT backbone but fell back to MinerU: {ppt_detection_reason}"
                    )

                # Parse document with MinerU
                logger.info(f"Parsing document {doc_id} with MinerU")
                mineru_input_doc = doc_path
                if doc_path.suffix.lower() == ".pptx":
                    augmented_path = ppt_notes_context.get("augmented_pptx_path")
                    if augmented_path:
                        mineru_input_doc = Path(str(augmented_path))
                self._run_mineru_on_document(
                    mineru_input_doc,
                    doc_output_dir,
                    method,
                    pp.get("lang"),
                    device=pp.get("device"),
                    backend=pp.get("backend"),
                    formula=pp.get("formula", True),
                    table=pp.get("table", True),
                    export_notes_pages=bool(pp.get("ppt_notes_export_for_mineru", True)),
                    slide_notes_map=ppt_notes_context.get("slide_notes_map", {}),
                )

                # Load and convert results
                asset, segments = self._load_existing_results(
                    doc_output_dir,
                    doc_path,
                    doc_id,
                    mid_path=mid_path,
                )
                # 如果上游传入 title（来自 asset_status.metadata），则覆盖写入到 index.json 的 title
                if pp.get("title"):
                    override_title = str(pp.get("title"))
                    asset.title = override_title
                    # Keep image/table caption_text in sync with overridden title.
                    self._apply_override_title_to_caption_segments(segments, override_title)
                if doc_path.suffix.lower() == ".pptx":
                    asset.extra["ppt_transcribed_media_count"] = int(
                        ppt_notes_context.get("transcribed_media_count", 0) or 0
                    )
                    asset.extra["ppt_transcribed_slide_count"] = int(
                        ppt_notes_context.get("transcribed_slide_count", 0) or 0
                    )
                    asset.extra["ppt_notes_export_for_mineru"] = bool(
                        pp.get("ppt_notes_export_for_mineru", True)
                    )
                    augmented_path = ppt_notes_context.get("augmented_pptx_path")
                    if augmented_path:
                        asset.extra["ppt_augmented_source_path"] = str(augmented_path)
                # 写入 index.json 缓存快照：对齐 video_parser 行为
                try:
                    payload = {
                        "doc_id": doc_id,
                        "asset": asset.model_dump(),
                        "segments": [s.model_dump() for s in segments],
                    }
                    method_dir = self._resolve_method_dir(doc_output_dir, doc_path.stem, mid_path)
                    index_file = method_dir / "index.json"
                    method_dir.mkdir(parents=True, exist_ok=True)
                    with open(index_file, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"MinerUParser: failed to write index.json: {e}")
                return asset, segments

            except Exception as e:
                logger.exception(f"Error parsing document {doc_id}: {e}")
                raise

    @staticmethod
    def _normalize_ppt_backbone(value: Any) -> str:
        text = str(value or "mineru").strip().lower()
        return text if text in {"mineru", "visual"} else "mineru"

    def _should_use_visual_ppt_backbone(
        self, doc_path: Path, ppt_backbone: str
    ) -> Tuple[bool, str]:
        if ppt_backbone != "visual":
            return False, "ppt_backbone=mineru"

        ext = doc_path.suffix.lower()
        if ext in self.PPT_FORMATS:
            return True, f"extension:{ext}"
        if ext != ".pdf":
            return False, f"unsupported_extension:{ext}"
        return self._detect_ppt_origin_pdf(doc_path)

    def _detect_ppt_origin_pdf(self, pdf_path: Path) -> Tuple[bool, str]:
        try:
            try:
                from pypdf import PdfReader  # type: ignore
            except ImportError:
                from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return False, "pdf_reader_unavailable"

        try:
            reader = PdfReader(str(pdf_path))
        except Exception as e:
            logger.warning(f"Failed to inspect PDF metadata for {pdf_path.name}: {e}")
            return False, f"pdf_open_failed:{type(e).__name__}"

        metadata_texts: List[str] = []
        try:
            metadata = reader.metadata or {}
            for key, value in metadata.items():
                if value is None:
                    continue
                metadata_texts.append(f"{key}={value}")
        except Exception as e:
            logger.debug(f"Failed to read PDF metadata for {pdf_path.name}: {e}")

        metadata_blob = " ".join(metadata_texts).casefold()
        for marker in ("microsoft powerpoint", "powerpoint", "microsoft\u00ae powerpoint\u00ae"):
            if marker in metadata_blob:
                return True, f"pdf_metadata:{marker}"

        page_sizes: List[Tuple[float, float]] = []
        try:
            for page_idx, page in enumerate(reader.pages):
                mediabox = getattr(page, "mediabox", None)
                if mediabox is None:
                    continue
                width = float(mediabox.width)
                height = float(mediabox.height)
                if width <= 0 or height <= 0:
                    continue
                page_sizes.append((width, height))
                if page_idx >= 19:
                    break
        except Exception as e:
            logger.debug(f"Failed to inspect PDF page sizes for {pdf_path.name}: {e}")

        if not page_sizes:
            return False, "no_page_geometry"

        base_width, base_height = page_sizes[0]
        consistent_pages = sum(
            1
            for width, height in page_sizes
            if abs(width - base_width) <= 2.0 and abs(height - base_height) <= 2.0
        )
        consistent_ratio = consistent_pages / max(len(page_sizes), 1)
        landscape_ratio = sum(1 for width, height in page_sizes if width > height) / max(
            len(page_sizes), 1
        )
        presentation_ratio = sum(
            1
            for width, height in page_sizes
            if height > 0
            and any(
                abs((width / height) - expected) <= 0.2 for expected in self.PPT_LANDSCAPE_RATIOS
            )
        ) / max(len(page_sizes), 1)

        if consistent_ratio >= 0.9 and landscape_ratio >= 0.8 and presentation_ratio >= 0.8:
            return (
                True,
                "pdf_layout:consistent_landscape_presentation_ratio",
            )

        return (
            False,
            "pdf_layout:no_ppt_signal"
            f"(consistent={consistent_ratio:.2f},landscape={landscape_ratio:.2f},presentation={presentation_ratio:.2f})",
        )

    @staticmethod
    def _normalize_note_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _merge_speaker_notes(cls, existing: str, transcript: str) -> str:
        existing_clean = str(existing or "").strip()
        transcript_clean = cls._normalize_note_text(transcript)
        if not transcript_clean:
            return existing_clean
        if transcript_clean in existing_clean:
            return existing_clean
        if existing_clean:
            return f"{existing_clean}\n\n{transcript_clean}".strip()
        return transcript_clean

    @classmethod
    def _compose_visual_caption_with_speaker_notes(
        cls, base_caption: str, speaker_notes: str
    ) -> str:
        base = str(base_caption or "").strip()
        notes = cls._normalize_note_text(speaker_notes)
        if not notes:
            return base
        suffix = f"#speaker notes: {notes}"
        if not base:
            return suffix
        return f"{base}\n{suffix}".strip()

    @classmethod
    def _normalize_ppt_asr_lang(cls, value: Any) -> str:
        text = str(value or "zh").strip().lower()
        if text in {"ch", "zh-cn", "zh_hans", "zh-hans"}:
            return "zh"
        return text or "zh"

    @staticmethod
    def _normalize_hotwords(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return []

    @staticmethod
    def _build_asr_prompt(hotwords: List[str]) -> Optional[str]:
        if not hotwords:
            return None
        return "需要注意以下术语:\n" + ", ".join(hotwords)

    @staticmethod
    def _normalize_zip_target(source_part_file: str, target: str) -> str:
        # In OPC, relationship Target is resolved relative to the source part,
        # not relative to the .rels file location.
        base_dir = posixpath.dirname(source_part_file)
        return posixpath.normpath(posixpath.join(base_dir, target)).lstrip("/")

    @classmethod
    def _is_supported_ppt_embedded_media(cls, target_path: str, rel_type: str) -> bool:
        ext = Path(target_path).suffix.lower()
        if ext in cls.PPT_EMBEDDED_AUDIO_FORMATS or ext in cls.PPT_EMBEDDED_VIDEO_FORMATS:
            return True
        rel_type_lower = str(rel_type or "").lower()
        return rel_type_lower.endswith("/audio") or rel_type_lower.endswith("/video")

    @classmethod
    def _extract_pptx_embedded_media(
        cls,
        pptx_path: Path,
        extract_dir: Path,
    ) -> Dict[int, List[Path]]:
        extract_dir.mkdir(parents=True, exist_ok=True)
        ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

        slide_to_media: Dict[int, List[Path]] = {}
        hash_to_file: Dict[str, Path] = {}
        with zipfile.ZipFile(pptx_path, "r") as zf:
            slide_xml_files = [
                name
                for name in zf.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ]
            slide_xml_files.sort(
                key=lambda x: (
                    int(re.search(r"slide(\d+)\.xml$", x).group(1))
                    if re.search(r"slide(\d+)\.xml$", x)
                    else 0
                )
            )

            for slide_file in slide_xml_files:
                match = re.search(r"slide(\d+)\.xml$", slide_file)
                if not match:
                    continue
                slide_idx = int(match.group(1)) - 1
                rels_file = (
                    posixpath.dirname(slide_file)
                    + "/_rels/"
                    + posixpath.basename(slide_file)
                    + ".rels"
                )
                if rels_file not in zf.namelist():
                    continue

                rels_root = ET.fromstring(zf.read(rels_file))
                media_paths: List[Path] = []
                slide_seen_media_files: set[str] = set()
                for rel in rels_root.findall("rel:Relationship", ns):
                    rel_type = str(rel.attrib.get("Type", ""))
                    target = str(rel.attrib.get("Target", ""))
                    if not target:
                        continue
                    normalized_target = cls._normalize_zip_target(slide_file, target)
                    if not cls._is_supported_ppt_embedded_media(normalized_target, rel_type):
                        continue
                    if normalized_target not in zf.namelist():
                        continue

                    raw = zf.read(normalized_target)
                    digest = hashlib.sha1(raw).hexdigest()
                    cached_file = hash_to_file.get(digest)
                    if cached_file is None:
                        ext = Path(normalized_target).suffix.lower() or ".bin"
                        cached_file = extract_dir / f"{digest}{ext}"
                        with open(cached_file, "wb") as f:
                            f.write(raw)
                        hash_to_file[digest] = cached_file
                    # A slide can contain multiple relationships that point to the same media.
                    # Keep one media file per slide to avoid duplicated ASR transcripts.
                    cache_key = str(cached_file)
                    if cache_key in slide_seen_media_files:
                        continue
                    slide_seen_media_files.add(cache_key)
                    media_paths.append(cached_file)

                if media_paths:
                    slide_to_media[slide_idx] = media_paths
        return slide_to_media

    @classmethod
    def _convert_media_to_asr_audio(cls, media_path: Path, output_audio_path: Path) -> None:
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        ext = media_path.suffix.lower()
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-map",
            "0:a:0",
        ]
        if ext in cls.PPT_EMBEDDED_VIDEO_FORMATS:
            cmd.extend(["-vn"])
        cmd.extend(["-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(output_audio_path)])

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError as e:
            raise RuntimeError("ffmpeg not found. Please install ffmpeg.") from e

    @classmethod
    def _build_slide_notes_text_map(
        cls,
        pptx_path: Path,
        slide_notes_map: Optional[Dict[Any, Any]] = None,
    ) -> Dict[int, str]:
        merged: Dict[int, str] = {}
        if isinstance(slide_notes_map, dict):
            for k, v in slide_notes_map.items():
                try:
                    idx = int(k)
                except Exception:
                    continue
                text = cls._normalize_note_text(str(v or ""))
                if text:
                    merged[idx] = text

        try:
            from pptx import Presentation  # type: ignore
        except ImportError:
            return merged

        prs = Presentation(str(pptx_path))
        for idx, slide in enumerate(prs.slides):
            text = merged.get(idx, "")
            if not text:
                try:
                    if getattr(slide, "has_notes_slide", False):
                        text = str(slide.notes_slide.notes_text_frame.text or "")
                except Exception:
                    text = ""
                text = cls._normalize_note_text(text)
            if text:
                merged[idx] = text

        return merged

    @classmethod
    def _inject_speaker_notes_textboxes_for_pdf(
        cls,
        src_pptx_path: Path,
        output_pptx_path: Path,
        slide_notes_map: Optional[Dict[Any, Any]] = None,
    ) -> bool:
        """Inject notes into slide textboxes to ensure PDF conversion includes notes text."""
        try:
            from pptx import Presentation  # type: ignore
            from pptx.dml.color import RGBColor  # type: ignore
            from pptx.util import Pt  # type: ignore
        except ImportError:
            logger.warning("pypptx-with-oxml is unavailable, skip speaker notes textbox injection")
            return False

        notes_map = cls._build_slide_notes_text_map(src_pptx_path, slide_notes_map)
        if not notes_map:
            return False

        prs = Presentation(str(src_pptx_path))
        injected_any = False
        for idx, slide in enumerate(prs.slides):
            notes_text = cls._normalize_note_text(notes_map.get(idx, ""))
            if not notes_text:
                continue

            text = f"#speaker notes: {notes_text}"
            slide_w = int(prs.slide_width)
            slide_h = int(prs.slide_height)
            margin = max(int(slide_w * 0.02), 1)
            box_h = max(int(slide_h * 0.18), 1)
            left = margin
            top = max(slide_h - box_h - margin, 0)
            width = max(slide_w - margin * 2, 1)
            height = box_h

            shape = slide.shapes.add_textbox(left, top, width, height)
            tf = shape.text_frame
            tf.clear()
            p = tf.paragraphs[0]
            p.text = text
            p.font.size = Pt(9)
            p.font.bold = False
            p.font.color.rgb = RGBColor(0, 0, 0)
            tf.word_wrap = True
            injected_any = True

        if not injected_any:
            return False

        output_pptx_path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(str(output_pptx_path))
        return True

    def _transcribe_audio_file_with_whisper_api(
        self,
        *,
        client: OpenAI,
        audio_path: Path,
        lang: str,
        hotwords: List[str],
    ) -> str:
        asr_prompt = self._build_asr_prompt(hotwords)
        with open(audio_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=f,
                model="vllm/whisper-large-v3",
                language=lang,
                response_format="verbose_json",
                prompt=asr_prompt,
            )

        pieces: List[str] = []
        segments = getattr(transcription, "segments", None)
        if segments:
            for seg in segments:
                text = ""
                if isinstance(seg, dict):
                    text = str(seg.get("text", "") or "")
                else:
                    text = str(getattr(seg, "text", "") or "")
                text = self._normalize_note_text(text)
                if text:
                    pieces.append(text)
        else:
            text = self._normalize_note_text(str(getattr(transcription, "text", "") or ""))
            if text:
                pieces.append(text)

        return " ".join(pieces).strip()

    def _collect_slide_notes_map(self, pptx_path: Path) -> Dict[int, str]:
        try:
            from pptx import Presentation  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "pypptx-with-oxml is required for PPT speaker notes processing"
            ) from e

        prs = Presentation(str(pptx_path))
        notes_map: Dict[int, str] = {}
        for idx, slide in enumerate(prs.slides):
            text = ""
            try:
                if getattr(slide, "has_notes_slide", False):
                    text = str(slide.notes_slide.notes_text_frame.text or "")
            except Exception:
                text = ""
            text = self._normalize_note_text(text)
            if text:
                notes_map[idx] = text
        return notes_map

    def _prepare_pptx_media_transcript_notes(
        self,
        *,
        doc_id: str,
        doc_path: Path,
        mineru_dir: Path,
        parser_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        enabled = bool(parser_params.get("ppt_media_asr_enabled", True))
        if doc_path.suffix.lower() != ".pptx":
            return {
                "augmented_pptx_path": None,
                "slide_notes_map": {},
                "transcribed_media_count": 0,
                "transcribed_slide_count": 0,
            }

        if not enabled:
            return {
                "augmented_pptx_path": None,
                "slide_notes_map": self._collect_slide_notes_map(doc_path),
                "transcribed_media_count": 0,
                "transcribed_slide_count": 0,
            }

        lang = self._normalize_ppt_asr_lang(parser_params.get("lang"))
        hotwords = self._normalize_hotwords(parser_params.get("asr_hotwords"))

        work_dir = mineru_dir / doc_id / "ppt_media_cache"
        media_dir = work_dir / "media"
        audio_dir = work_dir / "audio"
        work_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)

        slide_media_map = self._extract_pptx_embedded_media(doc_path, media_dir)
        if not slide_media_map:
            return {
                "augmented_pptx_path": None,
                "slide_notes_map": self._collect_slide_notes_map(doc_path),
                "transcribed_media_count": 0,
                "transcribed_slide_count": 0,
            }

        client = OpenAI(**self._resolve_whisper_client_kwargs(parser_params))

        slide_transcripts: Dict[int, List[str]] = {}
        media_transcript_cache: Dict[str, str] = {}
        transcribed_media_count = 0
        for slide_idx, media_files in slide_media_map.items():
            for media_file in media_files:
                try:
                    cache_key = str(media_file)
                    transcript_text = media_transcript_cache.get(cache_key, "")
                    if not transcript_text:
                        digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:16]
                        audio_path = audio_dir / f"{digest}.wav"
                        self._convert_media_to_asr_audio(media_file, audio_path)
                        transcript_text = self._transcribe_audio_file_with_whisper_api(
                            client=client,
                            audio_path=audio_path,
                            lang=lang,
                            hotwords=hotwords,
                        )
                        media_transcript_cache[cache_key] = transcript_text
                    transcript_text = self._normalize_note_text(transcript_text)
                    if transcript_text:
                        slide_transcripts.setdefault(slide_idx, []).append(transcript_text)
                        transcribed_media_count += 1
                except Exception as e:
                    logger.warning(
                        f"PPT media transcription failed for {media_file.name} (slide={slide_idx + 1}): {e}"
                    )

        if not slide_transcripts:
            return {
                "augmented_pptx_path": None,
                "slide_notes_map": self._collect_slide_notes_map(doc_path),
                "transcribed_media_count": 0,
                "transcribed_slide_count": 0,
            }

        try:
            from pptx import Presentation  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "pypptx-with-oxml is required for PPT speaker notes processing"
            ) from e

        prs = Presentation(str(doc_path))
        slide_notes_map: Dict[int, str] = {}
        changed = False

        for idx, slide in enumerate(prs.slides):
            existing = ""
            try:
                if getattr(slide, "has_notes_slide", False):
                    existing = str(slide.notes_slide.notes_text_frame.text or "")
            except Exception:
                existing = ""

            transcript = " ".join(slide_transcripts.get(idx, [])).strip()
            merged = self._merge_speaker_notes(existing, transcript)
            normalized_merged = self._normalize_note_text(merged)
            if normalized_merged:
                slide_notes_map[idx] = normalized_merged

            if transcript and merged != str(existing or "").strip():
                notes_slide = slide.notes_slide
                notes_slide.notes_text_frame.clear()
                notes_slide.notes_text_frame.text = merged
                changed = True

        augmented_path: Optional[Path] = None
        if changed:
            augmented_dir = mineru_dir / doc_id / "ppt_augmented"
            augmented_dir.mkdir(parents=True, exist_ok=True)
            augmented_path = augmented_dir / doc_path.name
            prs.save(str(augmented_path))

        if not slide_notes_map:
            slide_notes_map = self._collect_slide_notes_map(
                augmented_path if augmented_path is not None else doc_path
            )

        return {
            "augmented_pptx_path": str(augmented_path) if augmented_path else None,
            "slide_notes_map": slide_notes_map,
            "transcribed_media_count": transcribed_media_count,
            "transcribed_slide_count": len(slide_transcripts),
        }

    def _is_already_parsed(self, output_dir: Path, file_stem: str, mid_path: str) -> bool:
        """
        Check if document is already parsed by verifying required files exist.

        Args:
            output_dir: MinerU output directory
            file_stem: Document file name without extension
            mid_path: MinerU mid path (vlm or auto)

        Returns:
            True if already parsed with complete files
        """
        method_dir = self._resolve_method_dir(output_dir, file_stem, mid_path)
        if not method_dir.exists():
            return False

        # Check for required files
        content_list_files = list(method_dir.glob("*_content_list.json"))
        middle_json_files = list(method_dir.glob("*_middle.json"))

        return len(content_list_files) > 0 and len(middle_json_files) > 0

    @staticmethod
    def _resolve_method_dir(output_dir: Path, file_stem: str, mid_path: str) -> Path:
        """Resolve MinerU method directory across both layout styles.

        Preferred when exists:
        1) <output_dir>/<mid_path>          (Excel/custom layout)
        2) <output_dir>/<file_stem>/<mid_path> (MinerU default layout)

        If neither exists, return legacy MinerU default target path.
        """
        direct_dir = output_dir / mid_path
        if direct_dir.exists():
            return direct_dir

        legacy_dir = output_dir / file_stem / mid_path
        if legacy_dir.exists():
            return legacy_dir

        return legacy_dir

    def _parse_visual_ppt_document(
        self,
        doc_id: str,
        doc_path: Path,
        doc_output_dir: Path,
        parser_params: Optional[Dict[str, Any]] = None,
        detection_reason: str = "",
    ) -> Tuple[Asset, List[Segment]]:
        """Render PPT/PPT-origin PDF pages to images and emit MinerU-compatible artifacts."""
        mid_path = self._resolve_mineru_mid_path((parser_params or {}).get("backend"))
        method_dir = doc_output_dir / mid_path
        images_dir = method_dir / "images"
        method_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        ext = doc_path.suffix.lower()
        pdf_path = (
            doc_path
            if ext == ".pdf"
            else self._convert_office_to_pdf(doc_path, doc_output_dir.parent)
        )

        render_scale = float((parser_params or {}).get("ppt_visual_scale") or 2.0)
        rendered_pages = self._render_pdf_pages_to_images(
            pdf_path=pdf_path,
            images_dir=images_dir,
            scale=render_scale,
        )
        if not rendered_pages:
            raise RuntimeError(f"Failed to render any PPT pages from {doc_path}")

        segments: List[Segment] = []
        content_items: List[Dict[str, Any]] = []
        middle_pages: List[Dict[str, Any]] = []
        slide_notes_map = (parser_params or {}).get("ppt_slide_notes_map") or {}

        for page_idx, page_info in enumerate(rendered_pages):
            page_width = float(page_info["width"])
            page_height = float(page_info["height"])
            bbox = [0.0, 0.0, page_width, page_height]
            image_rel_path = f"images/{page_info['image_name']}"
            speaker_notes = ""
            if isinstance(slide_notes_map, dict):
                speaker_notes = str(
                    slide_notes_map.get(page_idx) or slide_notes_map.get(str(page_idx)) or ""
                )
            caption_text = self._compose_visual_caption_with_speaker_notes("", speaker_notes)

            segment = Segment(
                segment_id=f"{doc_id}_seg_{page_idx:04d}",
                doc_id=doc_id,
                order=page_idx,
                type="image",
                text=caption_text,
                location={"page": page_idx, "bbox": bbox},
                extra={
                    "img_path": image_rel_path,
                    "caption": caption_text,
                    "footnote": "",
                    "source": "ppt_visual",
                    "page_number": page_idx + 1,
                },
            )
            segments.append(segment)

            content_items.append(
                {
                    "type": "image",
                    "img_path": image_rel_path,
                    "img_relpath": image_rel_path,
                    "image_caption": [caption_text] if caption_text else [],
                    "image_footnote": [],
                    "page_idx": page_idx,
                    "bbox": bbox,
                }
            )
            middle_pages.append(
                {
                    "page_idx": page_idx,
                    "page_size": [page_width, page_height],
                    "para_blocks": [{"type": "image", "bbox": bbox, "blocks": []}],
                }
            )

        raw_lang = str((parser_params or {}).get("lang") or "").strip().lower()
        language = "zh" if raw_lang in {"", "ch", "zh"} else raw_lang
        title = (parser_params or {}).get("title") or doc_path.stem
        file_hash = self._calculate_file_hash(doc_path)
        asset = Asset(
            doc_id=doc_id,
            source_uri=str(doc_path),
            modality="document",
            file_hash=file_hash,
            created_at=int(time.time()),
            segments_count=len(segments),
            title=str(title),
            language=language,
            extra={
                "mineru_output_dir": str(doc_output_dir.parent),
                "content_list_file": str(method_dir / f"{doc_path.stem}_content_list.json"),
                "middle_json_file": str(method_dir / f"{doc_path.stem}_middle.json"),
                "total_pages": len(rendered_pages),
                "source_parser": "ppt_visual",
                "ppt_origin_detection": detection_reason,
                "visual_backbone_pdf_path": str(pdf_path),
                "ppt_notes_slide_count": len(
                    [k for k, v in (slide_notes_map or {}).items() if str(v).strip()]
                ),
            },
            page_info={
                "total_pages": len(rendered_pages),
                "page_sizes": {str(page["page_idx"]): page["page_size"] for page in middle_pages},
            },
        )

        self._write_excel_compat_artifacts(
            method_dir=method_dir,
            doc_path=doc_path,
            doc_id=doc_id,
            asset=asset,
            segments=segments,
            content_items=content_items,
            middle_pages=middle_pages,
        )
        return asset, segments

    def _render_pdf_pages_to_images(
        self, pdf_path: Path, images_dir: Path, scale: float = 2.0
    ) -> List[Dict[str, Any]]:
        try:
            import pypdfium2 as pdfium  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "pypdfium2 is required for visual PPT parsing but is not available"
            ) from e

        images_dir.mkdir(parents=True, exist_ok=True)
        rendered_pages: List[Dict[str, Any]] = []
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                bitmap = None
                try:
                    width, height = page.get_size()
                    bitmap = page.render(scale=max(scale, 1.0))
                    pil_image = bitmap.to_pil()
                    image_name = f"page_{page_idx:04d}.png"
                    image_path = images_dir / image_name
                    pil_image.save(image_path, format="PNG")
                    rendered_pages.append(
                        {
                            "page_idx": page_idx,
                            "image_name": image_name,
                            "image_path": image_path,
                            "width": float(width),
                            "height": float(height),
                        }
                    )
                finally:
                    if bitmap is not None and hasattr(bitmap, "close"):
                        bitmap.close()
                    if hasattr(page, "close"):
                        page.close()
        finally:
            if hasattr(pdf, "close"):
                pdf.close()

        return rendered_pages

    def _parse_excel_document(
        self,
        doc_id: str,
        doc_path: Path,
        doc_output_dir: Path,
        parser_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Asset, List[Segment]]:
        """Parse Excel directly and emit MinerU-compatible cache artifacts."""
        try:
            from openpyxl import load_workbook
            from openpyxl.utils.cell import range_boundaries
        except ImportError as e:
            raise RuntimeError(
                "openpyxl is required for Excel parsing. Please install it via uv/pip."
            ) from e

        try:
            wb = load_workbook(filename=str(doc_path), data_only=True)
        except Exception as e:
            raise RuntimeError(f"Failed to load Excel file: {doc_path}") from e

        mid_path = self._resolve_mineru_mid_path((parser_params or {}).get("backend"))
        method_dir = doc_output_dir / mid_path
        images_dir = method_dir / "images"
        method_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        segments: List[Segment] = []
        content_items: List[Dict[str, Any]] = []
        middle_pages: List[Dict[str, Any]] = []
        seg_order = 0

        for sheet_idx, ws in enumerate(wb.worksheets):
            self._expand_merged_cells(ws)
            max_row = int(ws.max_row or 0)
            max_col = int(ws.max_column or 0)
            para_blocks: List[Dict[str, Any]] = []
            table_seq = -1
            prev_non_empty_row_idx = -1
            first_markdown_row_in_table = False

            for row_idx in range(max_row):
                row_values: List[str] = []
                for col_idx in range(max_col):
                    value = ws.cell(row=row_idx + 1, column=col_idx + 1).value
                    if value is None:
                        row_values.append("")
                    else:
                        row_values.append(str(value).replace("\n", " ").strip())

                # Use effective row span (trim both left/right blanks) for frontend highlight.
                first_non_empty = -1
                last_non_empty = -1
                for idx_v, v in enumerate(row_values):
                    if v:
                        if first_non_empty < 0:
                            first_non_empty = idx_v
                        last_non_empty = idx_v
                if last_non_empty < 0:
                    continue

                # Detect independent table boundary by row-gap.
                if prev_non_empty_row_idx < 0 or (row_idx - prev_non_empty_row_idx) > 1:
                    table_seq += 1
                    first_markdown_row_in_table = True
                table_id = f"sheet_{sheet_idx}_table_{table_seq}"

                col_start = max(first_non_empty, 0)
                col_end = last_non_empty + 1
                effective_values = row_values[col_start:col_end]
                col_count = len(effective_values)

                row_text = "| " + " | ".join(v if v else " " for v in effective_values) + " |"
                if first_markdown_row_in_table:
                    header_sep = "| " + " | ".join(":---" for _ in range(col_count)) + " |"
                    row_text = f"{row_text}\n{header_sep}\n"
                    first_markdown_row_in_table = False
                else:
                    row_text = f"{row_text}\n"

                segment_id = f"{doc_id}_seg_{seg_order:04d}"
                seg_order += 1
                seg = Segment(
                    segment_id=segment_id,
                    doc_id=doc_id,
                    order=len(segments),
                    type="text",
                    text=row_text,
                    location={
                        "page": sheet_idx,
                        "row": row_idx,
                        "col_start": col_start,
                        "col_end": col_end,
                        "table_id": table_id,
                    },
                    extra={
                        "source": "excel",
                        "excel_location": [sheet_idx, row_idx, col_start, col_end],
                        "excel_table_id": table_id,
                        "sheet_name": ws.title,
                    },
                )
                segments.append(seg)
                prev_non_empty_row_idx = row_idx

                bbox = [
                    float(col_start) * 100.0,
                    float(row_idx) * 20.0,
                    float(max(col_start + 1, col_end)) * 100.0,
                    float(row_idx + 1) * 20.0,
                ]
                content_items.append(
                    {
                        "type": "text",
                        "text": row_text,
                        "text_level": 0,
                        "page_idx": sheet_idx,
                        "bbox": bbox,
                    }
                )
                para_blocks.append({"type": "text", "bbox": bbox, "blocks": []})

            image_segments, image_content_items, image_para_blocks = self._extract_excel_images(
                doc_path=doc_path,
                ws=ws,
                sheet_idx=sheet_idx,
                doc_id=doc_id,
                start_order=len(segments),
                images_dir=images_dir,
                range_boundaries_func=range_boundaries,
            )
            segments.extend(image_segments)
            content_items.extend(image_content_items)
            para_blocks.extend(image_para_blocks)

            middle_pages.append(
                {
                    "page_idx": sheet_idx,
                    "page_size": [float(max(1, max_col)) * 100.0, float(max(1, max_row)) * 20.0],
                    "para_blocks": para_blocks,
                }
            )

        file_hash = self._calculate_file_hash(doc_path)
        title = (parser_params or {}).get("title") or doc_path.stem
        asset = Asset(
            doc_id=doc_id,
            source_uri=str(doc_path),
            modality="document",
            file_hash=file_hash,
            created_at=int(time.time()),
            segments_count=len(segments),
            title=str(title),
            language="zh",
            extra={
                # Keep root semantics to match ImageEnhancer path join logic.
                "mineru_output_dir": str(doc_output_dir.parent),
                "content_list_file": str(method_dir / f"{doc_path.stem}_content_list.json"),
                "middle_json_file": str(method_dir / f"{doc_path.stem}_middle.json"),
                "total_pages": len(wb.worksheets),
                "source_parser": "excel",
            },
            page_info={
                "total_pages": len(wb.worksheets),
                "page_sizes": {str(p["page_idx"]): p["page_size"] for p in middle_pages},
            },
        )

        self._write_excel_compat_artifacts(
            method_dir=method_dir,
            doc_path=doc_path,
            doc_id=doc_id,
            asset=asset,
            segments=segments,
            content_items=content_items,
            middle_pages=middle_pages,
        )
        return asset, segments

    @staticmethod
    def _expand_merged_cells(ws: Any) -> None:
        """Expand merged ranges by copying top-left value to every covered cell."""
        merged_ranges = list(ws.merged_cells.ranges)
        for cell_range in merged_ranges:
            min_col, min_row, max_col, max_row = cell_range.bounds
            value = ws.cell(row=min_row, column=min_col).value
            ws.unmerge_cells(str(cell_range))
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    ws.cell(row=row, column=col, value=value)

    def _extract_excel_images(
        self,
        *,
        doc_path: Path,
        ws: Any,
        sheet_idx: int,
        doc_id: str,
        start_order: int,
        images_dir: Path,
        range_boundaries_func: Any,
    ) -> Tuple[List[Segment], List[Dict[str, Any]], List[Dict[str, Any]]]:
        segments: List[Segment] = []
        content_items: List[Dict[str, Any]] = []
        para_blocks: List[Dict[str, Any]] = []
        seen_image_hashes: set[str] = set()

        def _append_image_segment(*, raw: bytes, row_id: int, idx: int) -> None:
            image_hash = hashlib.sha1(raw).hexdigest()
            if image_hash in seen_image_hashes:
                return
            seen_image_hashes.add(image_hash)

            image_rel_path = f"images/sheet_{sheet_idx}_img_{idx}.png"
            image_abs_path = images_dir / f"sheet_{sheet_idx}_img_{idx}.png"
            self._save_image_bytes(raw, image_abs_path)

            segment_id = f"{doc_id}_seg_{start_order + len(segments):04d}"
            seg = Segment(
                segment_id=segment_id,
                doc_id=doc_id,
                order=start_order + len(segments),
                type="image",
                text="",
                location={"page": sheet_idx, "row": row_id, "col_start": 0, "col_end": 0},
                extra={
                    "img_path": image_rel_path,
                    "source": "excel",
                    "excel_location": [sheet_idx, row_id, 0, 0],
                    "caption": "",
                    "footnote": "",
                },
            )
            segments.append(seg)

            bbox = [0.0, float(row_id) * 20.0, 120.0, float(row_id + 1) * 20.0]
            content_items.append(
                {
                    "type": "image",
                    "img_path": image_rel_path,
                    "image_caption": [],
                    "image_footnote": [],
                    "page_idx": sheet_idx,
                    "bbox": bbox,
                }
            )
            para_blocks.append({"type": "image", "bbox": bbox, "blocks": []})

        images = list(getattr(ws, "_images", []) or [])
        for idx, img in enumerate(images):
            row_id = 0
            anchor = getattr(img, "anchor", None)

            try:
                if hasattr(anchor, "_from"):
                    # oneCellAnchor / twoCellAnchor: openpyxl stores 0-based row index.
                    from_row = int(getattr(anchor._from, "row", 0))
                    row_id = max(from_row, 0)
                    if hasattr(anchor, "to") and getattr(anchor, "to") is not None:
                        to_row = int(getattr(anchor.to, "row", from_row))
                        row_id = max(min(from_row, to_row), 0)
                elif isinstance(anchor, str):
                    if ":" in anchor:
                        left, right = anchor.split(":", 1)
                        _, min_row, _, max_row = range_boundaries_func(f"{left}:{right}")
                        row_id = max(min(min_row, max_row) - 1, 0)
                    else:
                        _, min_row, _, _ = range_boundaries_func(f"{anchor}:{anchor}")
                        row_id = max(min_row - 1, 0)
            except Exception:
                row_id = 0

            try:
                raw = img._data() if hasattr(img, "_data") else None
                if raw:
                    _append_image_segment(raw=raw, row_id=row_id, idx=idx)
            except Exception as e:
                logger.warning(f"Excel image extraction (openpyxl) failed: {e}")

        # Fallback/补充: parse drawing XML directly from xlsx zip to capture "in-cell" images.
        try:
            zip_images = self._extract_excel_images_from_zip(doc_path=doc_path, ws=ws)
            base_idx = len(images)
            for rel_idx, item in enumerate(zip_images):
                raw = item.get("bytes")
                if not raw:
                    continue
                row_id = int(item.get("row", 0) or 0)
                _append_image_segment(raw=raw, row_id=max(row_id, 0), idx=base_idx + rel_idx)
        except Exception as e:
            logger.warning(f"Excel image extraction (zip fallback) failed: {e}")

        return segments, content_items, para_blocks

    @staticmethod
    def _save_image_bytes(raw: bytes, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw:
            raise RuntimeError("Failed to read Excel embedded image bytes")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as im:
                im.save(target_path, format="PNG")
                return
        except Exception:
            # Fallback: write raw bytes directly.
            with open(target_path, "wb") as f:
                f.write(raw)

    def _extract_excel_images_from_zip(self, *, doc_path: Path, ws: Any) -> List[Dict[str, Any]]:
        """Extract sheet images from raw xlsx drawing XML, including in-cell anchored images."""
        ns = {
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
            "ws": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }

        def _norm(base_file: str, target: str) -> str:
            base_dir = posixpath.dirname(base_file)
            return posixpath.normpath(posixpath.join(base_dir, target)).lstrip("/")

        ws_path = str(getattr(ws, "path", "") or "").lstrip("/")
        if not ws_path:
            return []
        if not ws_path.startswith("xl/"):
            ws_path = f"xl/{ws_path}"

        sheet_rels_path = (
            posixpath.dirname(ws_path) + "/_rels/" + posixpath.basename(ws_path) + ".rels"
        )

        out: List[Dict[str, Any]] = []
        with zipfile.ZipFile(doc_path, "r") as zf:
            if sheet_rels_path not in zf.namelist():
                return []

            rels_root = ET.fromstring(zf.read(sheet_rels_path))
            drawing_targets: List[str] = []
            for rel in rels_root.findall("rel:Relationship", ns):
                rel_type = str(rel.attrib.get("Type", ""))
                if rel_type.endswith("/drawing"):
                    target = rel.attrib.get("Target", "")
                    if target:
                        drawing_targets.append(_norm(sheet_rels_path, target))

            for drawing_path in drawing_targets:
                if drawing_path not in zf.namelist():
                    continue

                drawing_rels_path = (
                    posixpath.dirname(drawing_path)
                    + "/_rels/"
                    + posixpath.basename(drawing_path)
                    + ".rels"
                )
                rid_to_media: Dict[str, str] = {}
                if drawing_rels_path in zf.namelist():
                    drawing_rels_root = ET.fromstring(zf.read(drawing_rels_path))
                    for rel in drawing_rels_root.findall("rel:Relationship", ns):
                        rel_type = str(rel.attrib.get("Type", ""))
                        if rel_type.endswith("/image"):
                            rid = rel.attrib.get("Id")
                            target = rel.attrib.get("Target", "")
                            if rid and target:
                                rid_to_media[rid] = _norm(drawing_rels_path, target)

                drawing_root = ET.fromstring(zf.read(drawing_path))
                for anchor in drawing_root.findall("xdr:oneCellAnchor", ns) + drawing_root.findall(
                    "xdr:twoCellAnchor", ns
                ):
                    row_id = 0
                    frm = anchor.find("xdr:from", ns)
                    to = anchor.find("xdr:to", ns)
                    if frm is not None:
                        row_text = frm.findtext("xdr:row", default="0", namespaces=ns)
                        try:
                            row_id = max(int(row_text), 0)
                        except Exception:
                            row_id = 0
                    if to is not None:
                        to_row_text = to.findtext("xdr:row", default=str(row_id), namespaces=ns)
                        try:
                            row_id = max(min(row_id, int(to_row_text)), 0)
                        except Exception:
                            pass

                    blip = anchor.find(".//a:blip", ns)
                    if blip is None:
                        continue
                    rid = blip.attrib.get(f"{{{ns['r']}}}embed")
                    media_path = rid_to_media.get(str(rid)) if rid else None
                    if not media_path or media_path not in zf.namelist():
                        continue

                    out.append({"row": row_id, "bytes": zf.read(media_path)})

        return out

    @staticmethod
    def _write_excel_compat_artifacts(
        *,
        method_dir: Path,
        doc_path: Path,
        doc_id: str,
        asset: Asset,
        segments: List[Segment],
        content_items: List[Dict[str, Any]],
        middle_pages: List[Dict[str, Any]],
    ) -> None:
        index_file = method_dir / "index.json"
        content_file = method_dir / f"{doc_path.stem}_content_list.json"
        middle_file = method_dir / f"{doc_path.stem}_middle.json"
        md_file = method_dir / f"{doc_path.stem}.md"

        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "doc_id": doc_id,
                    "asset": asset.model_dump(),
                    "segments": [s.model_dump() for s in segments],
                },
                f,
                ensure_ascii=False,
            )

        with open(content_file, "w", encoding="utf-8") as f:
            json.dump(content_items, f, ensure_ascii=False)

        with open(middle_file, "w", encoding="utf-8") as f:
            json.dump({"pdf_info": middle_pages}, f, ensure_ascii=False)

        md_text = MinerUParser._render_compat_markdown(content_items)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_text)

    @staticmethod
    def _render_compat_markdown(content_items: List[Dict[str, Any]]) -> str:
        def _clean_text(value: Any) -> str:
            text = str(value or "")
            if not text:
                return ""
            return re.sub(r"\s+", " ", text).strip()

        def _normalize_markdown_block(text: str) -> str:
            lines = [line.rstrip() for line in str(text or "").splitlines()]
            lines = [line for line in lines if line.strip()]
            return "\n".join(lines).strip()

        blocks: List[str] = []
        for item in content_items:
            if not isinstance(item, dict):
                continue

            item_type = str(item.get("type") or "").lower()
            block = ""

            if item_type in {"text", "code", "equation"}:
                block = _normalize_markdown_block(item.get("text") or item.get("code") or "")
            elif item_type == "list":
                list_items = item.get("list_items")
                if isinstance(list_items, list):
                    entries = [
                        f"- {_clean_text(entry)}" for entry in list_items if _clean_text(entry)
                    ]
                    block = "\n".join(entries)
                else:
                    block = _normalize_markdown_block(item.get("text") or list_items or "")
            elif item_type == "table":
                parts: List[str] = []
                captions = item.get("table_caption") or item.get("caption") or []
                footnotes = item.get("table_footnote") or item.get("footnote") or []
                if isinstance(captions, list):
                    parts.extend(_clean_text(entry) for entry in captions if _clean_text(entry))
                elif captions:
                    parts.append(_clean_text(captions))

                table_body = str(item.get("table_body") or "")
                if table_body:
                    if "<" in table_body and ">" in table_body:
                        table_body = re.sub(r"<[^>]+>", " ", table_body)
                    table_body = _clean_text(table_body)
                    if table_body:
                        parts.append(table_body)

                if isinstance(footnotes, list):
                    parts.extend(_clean_text(entry) for entry in footnotes if _clean_text(entry))
                elif footnotes:
                    parts.append(_clean_text(footnotes))

                block = "\n".join(part for part in parts if part)
            elif item_type == "image":
                parts = []
                captions = item.get("image_caption", item.get("img_caption", []))
                footnotes = item.get("image_footnote", item.get("img_footnote", []))
                if isinstance(captions, list):
                    parts.extend(_clean_text(entry) for entry in captions if _clean_text(entry))
                elif captions:
                    parts.append(_clean_text(captions))
                if isinstance(footnotes, list):
                    parts.extend(_clean_text(entry) for entry in footnotes if _clean_text(entry))
                elif footnotes:
                    parts.append(_clean_text(footnotes))
                block = "\n".join(part for part in parts if part)

            if block:
                blocks.append(block)

        return "\n\n".join(blocks).strip() + ("\n" if blocks else "")

    def _run_mineru_on_document(
        self,
        doc_path: Path,
        output_dir: Path,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> None:
        """
        Run MinerU command on document based on file type.

        Args:
            doc_path: Path to document
            output_dir: Output directory
            method: Parsing method
            lang: Language for OCR
            **kwargs: Additional MinerU parameters
        """
        ext = doc_path.suffix.lower()
        export_notes_pages = bool(kwargs.pop("export_notes_pages", False))
        slide_notes_map = kwargs.pop("slide_notes_map", None)

        if ext == ".pdf":
            self._run_mineru_command(
                input_path=doc_path,
                output_dir=output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )
        elif ext in self.IMAGE_FORMATS:
            # Images require OCR method
            processed_path = self._prepare_image_for_mineru(doc_path)
            try:
                self._run_mineru_command(
                    input_path=processed_path,
                    output_dir=output_dir,
                    method="ocr",
                    lang=lang,
                    **kwargs,
                )
            finally:
                # Cleanup temporary converted file if created
                if processed_path != doc_path and processed_path.exists():
                    processed_path.unlink()
        elif ext in self.OFFICE_FORMATS:
            # Convert to PDF first
            should_export_notes = export_notes_pages and ext in self.PPT_FORMATS
            pdf_path = self._convert_office_to_pdf(
                doc_path,
                output_dir,
                export_notes_pages=should_export_notes,
                slide_notes_map=slide_notes_map,
            )
            self._run_mineru_command(
                input_path=pdf_path,
                output_dir=output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )
        elif ext in self.TEXT_FORMATS:
            # Convert to PDF first
            pdf_path = self._convert_office_to_pdf(doc_path, output_dir)
            self._run_mineru_command(
                input_path=pdf_path,
                output_dir=output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )
        else:
            # Try as PDF for unsupported formats
            logger.warning(f"Unsupported format {ext}, attempting to parse as PDF")
            self._run_mineru_command(
                input_path=doc_path,
                output_dir=output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )

    def _load_existing_results(
        self,
        output_dir: Path,
        file_path: Path,
        doc_id: str,
        *,
        mid_path: Optional[str] = None,
    ) -> Tuple[Asset, List[Segment]]:
        """
        Load existing MinerU results and convert to Asset and Segments.

        Args:
            output_dir: MinerU output directory
            file_stem: Original file stem
            doc_id: Document ID

        Returns:
            Tuple of (Asset, List[Segment])
        """
        mid_path = mid_path or self._resolve_mineru_mid_path()
        method_dir = self._resolve_method_dir(output_dir, file_path.stem, mid_path)

        index_json_file = method_dir / "index.json"
        if index_json_file.exists():
            try:
                with open(index_json_file, "r", encoding="utf-8") as f:
                    index_data = json.load(f)
                asset_data = index_data.get("asset")
                segment_data = index_data.get("segments")
                if isinstance(asset_data, dict) and isinstance(segment_data, list):
                    asset = Asset.model_validate(asset_data)
                    segments = [Segment.model_validate(seg) for seg in segment_data]
                    segments = self._normalize_parser_noise_segments(segments)
                    self._normalize_structured_caption_segments(segments)
                    asset.source_uri = str(file_path)
                    asset.segments_count = len(segments)
                    return asset, segments
            except Exception as e:
                logger.debug(f"Could not hydrate cached index.json snapshot: {e}")

        content_list_file = self._find_content_list_file(method_dir)
        middle_json_file = self._find_middle_json_file(method_dir)

        # Load content list
        with open(content_list_file, "r", encoding="utf-8") as f:
            content_items = json.load(f)

        # Load middle.json for bbox information
        try:
            with open(middle_json_file, "r", encoding="utf-8") as f:
                middle_data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load middle.json file: {e}")
            middle_data = {"pdf_info": []}

        # Calculate file hash
        file_hash = self._calculate_file_hash(file_path)

        # Extract metadata
        title = self._extract_title(content_items)
        language = self._detect_language(content_items)

        # Prefer cached index.json title when available (external pipelines may write it)
        doc_title = title
        if index_json_file.exists():
            try:
                with open(index_json_file, "r", encoding="utf-8") as f:
                    index_data = json.load(f)
                doc_title = (
                    index_data.get("asset", {}).get("title") or index_data.get("title") or doc_title
                )
            except Exception as e:
                logger.debug(f"Could not load index.json for doc_title: {e}")
        if doc_title:
            title = doc_title

        # Create Asset
        mineru_output_root = output_dir
        if output_dir.name == str(doc_id):
            mineru_output_root = output_dir.parent

        asset = Asset(
            doc_id=doc_id,
            source_uri=str(file_path),
            modality="document",
            file_hash=file_hash,
            created_at=int(time.time()),
            segments_count=len(content_items),
            title=title,
            language=language,
            extra={
                "mineru_output_dir": str(mineru_output_root),
                "content_list_file": str(content_list_file),
                "middle_json_file": str(middle_json_file),
                "total_pages": max((item.get("page_idx", 0) for item in content_items), default=0)
                + 1,
            },
            page_info={
                "total_pages": len(middle_data["pdf_info"]),
                "page_sizes": {
                    page["page_idx"]: page["page_size"] for page in middle_data["pdf_info"]
                },
            },
        )

        # Create Segments
        segments = self._create_segments_from_content(
            content_items, middle_data, doc_id, doc_title=doc_title or ""
        )
        self._normalize_structured_caption_segments(segments)

        # Update segments_count to actual count
        asset.segments_count = len(segments)

        return asset, segments

    @staticmethod
    def _rect_collision_distance(a: List[float], b: List[float]) -> float:
        """Euclidean distance between two axis-aligned bboxes (0 if overlapping).

        bboxes are [x0, y0, x1, y1].
        """
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        dx = max(ax0 - bx1, bx0 - ax1, 0.0)
        dy = max(ay0 - by1, by0 - ay1, 0.0)
        return (dx * dx + dy * dy) ** 0.5

    @staticmethod
    def _bbox_area(b: List[float]) -> float:
        x0, y0, x1, y1 = b
        return max(x1 - x0, 0.0) * max(y1 - y0, 0.0)

    @staticmethod
    def _bbox_overlap_ratios(a: List[float], b: List[float]) -> Tuple[float, float, float]:
        """Return (x_overlap_ratio, overlap_area_ratio, b_over_a_area).

        Ratios are normalized to be scale-free.
        - x_overlap_ratio: overlap width / min(width_a, width_b)
        - overlap_area_ratio: overlap area / min(area_a, area_b)
        - b_over_a_area: area_b / max(area_a, eps)
        """
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b

        aw = max(ax1 - ax0, 0.0)
        bw = max(bx1 - bx0, 0.0)
        ah = max(ay1 - ay0, 0.0)
        bh = max(by1 - by0, 0.0)

        ox = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        oy = max(0.0, min(ay1, by1) - max(ay0, by0))
        x_overlap_ratio = ox / max(min(aw, bw), 1e-6)

        overlap_area = ox * oy
        area_a = aw * ah
        area_b = bw * bh
        overlap_area_ratio = overlap_area / max(min(area_a, area_b), 1e-6)
        b_over_a_area = area_b / max(area_a, 1e-6)
        return x_overlap_ratio, overlap_area_ratio, b_over_a_area

    def _caption_candidate_score(
        self,
        *,
        fig_bbox: List[float],
        cand_bbox: List[float],
        cand_text: str,
    ) -> float:
        """Lower score is better."""
        score, _ = self._caption_candidate_components(
            fig_bbox=fig_bbox, cand_bbox=cand_bbox, cand_text=cand_text
        )
        return score

    def _caption_candidate_components(
        self,
        *,
        fig_bbox: List[float],
        cand_bbox: List[float],
        cand_text: str,
    ) -> Tuple[float, Dict[str, float]]:
        """Return (score, components) for debugging/tuning."""
        dist = self._rect_collision_distance(fig_bbox, cand_bbox)
        x_overlap_ratio, overlap_area_ratio, cand_over_fig_area = self._bbox_overlap_ratios(
            fig_bbox, cand_bbox
        )

        # Distance term with overlap penalty (PPT often has big text boxes overlapping figures).
        effective_dist = dist + overlap_area_ratio * self.CAPTION_OVERLAP_DISTANCE_SCALE

        # Soft preference for horizontal alignment (do NOT hard-filter).
        align_factor = 1.0 + self.CAPTION_ALIGN_WEIGHT * (1.0 - max(min(x_overlap_ratio, 1.0), 0.0))

        # Penalize very large text blocks relative to the figure/table.
        size_factor = 1.0
        if cand_over_fig_area > 1.0:
            size_factor = 1.0 + self.CAPTION_SIZE_WEIGHT * (cand_over_fig_area - 1.0)

        text = (cand_text or "").strip()
        newline_count = text.count("\n")
        bullet_hint = any(tok in text for tok in ("•", "·", "○", "- ", "-\t"))
        listiness = newline_count + (1 if bullet_hint else 0)
        newline_factor = 1.0 + self.CAPTION_NEWLINE_WEIGHT * float(listiness)

        length_over = max(len(text) - 100, 0)
        length_factor = 1.0 + self.CAPTION_LENGTH_WEIGHT * (length_over / 100.0)

        score = effective_dist * align_factor * size_factor * newline_factor * length_factor
        components = {
            "effective_dist": float(effective_dist),
            "align_factor": float(align_factor),
            "size_factor": float(size_factor),
            "newline_factor": float(newline_factor),
            "length_factor": float(length_factor),
        }
        return float(score), components

    @staticmethod
    def _find_prev_level1_title(content_items: List[Dict], current_index: int) -> str:
        """Find the nearest previous text item with text_level == 1."""
        for j in range(current_index - 1, -1, -1):
            it = content_items[j]
            if it.get("type") != "text":
                continue
            if it.get("text_level") != 1:
                continue
            text = str(it.get("text", "")).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _find_nearest_text_candidates(
        content_items: List[Dict],
        current_index: int,
        used_caption_text_indices: set,
        *,
        window: int = 12,
        max_candidates: int = 12,
    ) -> List[int]:
        """Return indices of nearby usable text items within a window.

        MinerU's reading order can be unreliable for PPT-like layouts, so we
        collect multiple nearby text candidates around the current index.
        """
        candidates: List[int] = []
        if window <= 0 or max_candidates <= 0:
            return candidates

        for offset in range(1, window + 1):
            for j in (current_index - offset, current_index + offset):
                if j < 0 or j >= len(content_items):
                    continue
                if j in used_caption_text_indices:
                    continue
                it = content_items[j]
                if it.get("type") != "text":
                    continue
                text = str(it.get("text", "")).strip()
                if not text:
                    continue
                candidates.append(j)
                if len(candidates) >= max_candidates:
                    return candidates

        return candidates

    def _find_content_list_file(self, auto_dir: Path) -> Path:
        """Find the content_list.json file in auto directory."""
        candidates = list(auto_dir.glob("*_content_list.json"))
        if not candidates:
            raise FileNotFoundError("No content_list.json file found in auto directory")
        return candidates[0]  # Take first match

    def _find_middle_json_file(self, auto_dir: Path) -> Path:
        """Find the middle.json file in auto directory."""
        candidates = list(auto_dir.glob("*_middle.json"))
        if not candidates:
            raise FileNotFoundError("No middle.json file found in auto directory")
        return candidates[0]  # Take first match

    def _find_original_file(self, output_dir: Path, file_stem: str) -> Optional[Path]:
        """Find the original file for hashing."""
        # Look for files in parent directory (same level as output folder)
        parent_dir = output_dir.parent

        # Try different extensions
        for ext in [
            ".pdf",
            ".docx",
            ".doc",
            ".pptx",
            ".ppt",
            ".xlsx",
            ".xls",
            ".txt",
            ".md",
        ]:
            candidate = parent_dir / f"{file_stem}{ext}"
            if candidate.exists():
                return candidate

        # Try in mid_path directory with origin prefix
        mid_path_dir = output_dir / HyperRAGConfig.mineru_mid_path
        origin_file = mid_path_dir / f"{file_stem}_origin.pdf"
        return origin_file if origin_file.exists() else None

    @staticmethod
    def _run_mineru_command(
        input_path: Union[str, Path],
        output_dir: Union[str, Path],
        method: str = "auto",
        lang: Optional[str] = "zh",
        backend: Optional[str] = "pipeline",
        formula: bool = True,
        table: bool = True,
        device: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        """
        同步执行 mineru 命令。超时后杀掉进程组，避免子进程卡住占住线程池和 GIL。
        """

        resolved_backend = backend or HyperRAGConfig.mineru_backend or "pipeline"

        # 构建基础命令
        cmd = [
            "mineru",
            "-p",
            str(input_path),
            "-o",
            str(output_dir),
            "-m",
            method,
            "-b",
            resolved_backend,
        ]

        # OCR 语言参数
        if lang:
            cmd.extend(["-l", lang])

        if formula:
            cmd.extend(["-f", "true"])
        if table:
            cmd.extend(["-t", "false"])

        if resolved_backend == "pipeline":
            if device:
                cmd.extend(["-d", device])

        timeout_seconds = mineru_timeout_seconds() if timeout is None else float(timeout)
        if timeout_seconds <= 0:
            raise ValueError("mineru timeout must be > 0 seconds")

        logger.info(f"[MinerU] Start executing ({timeout_seconds:.0f}s timeout): {' '.join(cmd)}")

        try:
            popen_kwargs: Dict[str, Any] = {
                "text": True,
                "encoding": "utf-8",
                "errors": "ignore",
            }
            if platform.system() == "Windows":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(cmd, **popen_kwargs)
            try:
                proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_mineru_process(proc)
                raise TimeoutError(
                    f"mineru exceeded {timeout_seconds:.0f}s: {' '.join(cmd)}"
                ) from None

            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd)

        except FileNotFoundError:
            raise RuntimeError(
                "mineru command not found. Please ensure MinerU is properly installed:\n"
                "pip install -U 'mineru[pipeline,vlm]' or uv pip install -U 'mineru[pipeline,vlm]'"
            )

    def _prepare_image_for_mineru(self, image_path: Path) -> Path:
        """
        Prepare image for MinerU processing (convert if needed).

        Args:
            image_path: Path to image file

        Returns:
            Path to processed image (may be original or converted)
        """
        # MinerU supported formats
        mineru_supported_formats = {".png", ".jpeg", ".jpg"}
        ext = image_path.suffix.lower()

        if ext in mineru_supported_formats:
            return image_path

        # Convert unsupported formats to PNG
        logger.info(f"Converting {ext} image to PNG for MinerU compatibility...")

        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError(
                "PIL/Pillow is required for image format conversion. "
                "Please install it using: pip install Pillow"
            )

        # Create temporary file
        temp_dir = Path(tempfile.mkdtemp())
        temp_file = temp_dir / f"{image_path.stem}_converted.png"

        try:
            # Open and convert image
            with Image.open(image_path) as img:
                # Handle different image modes
                if img.mode in ("RGBA", "LA", "P"):
                    # For images with transparency or palette, convert to RGB first
                    if img.mode == "P":
                        img = img.convert("RGBA")

                    # Create white background for transparent images
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "RGBA":
                        background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                    else:
                        background.paste(img)
                    img = background
                elif img.mode not in ("RGB", "L"):
                    # Convert other modes to RGB
                    img = img.convert("RGB")

                # Save as PNG
                img.save(temp_file, "PNG", optimize=True)
                logger.info(
                    f"Successfully converted {image_path.name} to PNG ({temp_file.stat().st_size / 1024:.1f} KB)"
                )

                return temp_file

        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            raise RuntimeError(f"Failed to convert image {image_path.name}: {str(e)}")

    @staticmethod
    def _build_libreoffice_convert_targets(doc_path: Path, export_notes_pages: bool) -> List[str]:
        ext = doc_path.suffix.lower()
        if export_notes_pages and ext in MinerUParser.PPT_FORMATS:
            return [
                'pdf:impress_pdf_Export:{"ExportNotesPages":{"type":"boolean","value":"true"}}',
                "pdf:impress_pdf_Export",
                "pdf",
            ]
        return ["pdf"]

    @staticmethod
    def _decode_text_document(text_path: Path) -> Tuple[str, str]:
        """Read a text document with UTF-8 first and common legacy fallbacks."""
        raw_bytes = text_path.read_bytes()
        if not raw_bytes:
            return "", "utf-8"

        for encoding in MinerUParser.TEXT_READ_ENCODINGS:
            try:
                return raw_bytes.decode(encoding), encoding
            except UnicodeDecodeError:
                continue

        raise RuntimeError(
            f"Could not decode text file {text_path.name} with supported encodings: {MinerUParser.TEXT_READ_ENCODINGS}"
        )

    @staticmethod
    def _prepare_text_for_libreoffice(text_path: Path, temp_dir: Path) -> Tuple[Path, str]:
        """Normalize legacy-encoded text files to UTF-8 before LibreOffice conversion."""
        text_content, source_encoding = MinerUParser._decode_text_document(text_path)
        normalized_encoding = source_encoding.lower()
        if normalized_encoding in {"utf-8", "utf-8-sig"}:
            return text_path, source_encoding

        normalized_path = temp_dir / text_path.name
        normalized_path.write_text(text_content, encoding="utf-8")
        logger.info(
            f"Normalized text document {text_path.name} from {source_encoding} to UTF-8 before LibreOffice conversion"
        )
        return normalized_path, source_encoding

    @staticmethod
    def _convert_office_to_pdf(
        doc_path: Path,
        output_dir: Path,
        export_notes_pages: bool = False,
        slide_notes_map: Optional[Dict[Any, Any]] = None,
    ) -> Path:
        """
        Convert Office document to PDF using LibreOffice.

        Args:
            doc_path: Path to Office document
            output_dir: Output directory

        Returns:
            Path to converted PDF
        """
        try:
            # Use LibreOffice for conversion
            import shutil

            name_without_suff = doc_path.stem

            # Create temporary directory for PDF conversion
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)

                # Convert to PDF using LibreOffice
                logger.info(f"Converting {doc_path.name} to PDF using LibreOffice...")

                convert_input_path = doc_path
                if doc_path.suffix.lower() in MinerUParser.TEXT_FORMATS:
                    convert_input_path, source_encoding = (
                        MinerUParser._prepare_text_for_libreoffice(doc_path, temp_path)
                    )
                    if convert_input_path != doc_path:
                        logger.info(
                            f"Using normalized UTF-8 copy for {doc_path.name} (detected source encoding: {source_encoding})"
                        )

                if export_notes_pages and doc_path.suffix.lower() == ".pptx":
                    overlay_input = temp_path / f"{name_without_suff}.notes_overlay.pptx"
                    try:
                        injected = MinerUParser._inject_speaker_notes_textboxes_for_pdf(
                            src_pptx_path=doc_path,
                            output_pptx_path=overlay_input,
                            slide_notes_map=slide_notes_map,
                        )
                        if injected:
                            convert_input_path = overlay_input
                            logger.info(
                                f"Using notes-overlay PPTX for PDF conversion: {convert_input_path.name}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to inject notes textboxes before PDF conversion: {e}"
                        )

                # Try LibreOffice commands in order of preference
                commands_to_try = ["libreoffice", "soffice"]

                conversion_successful = False
                generated_pdf_path: Optional[Path] = None
                convert_targets = MinerUParser._build_libreoffice_convert_targets(
                    doc_path, export_notes_pages=export_notes_pages
                )
                expected_pdf_candidates = [
                    temp_path / f"{convert_input_path.stem}.pdf",
                    temp_path / f"{name_without_suff}.pdf",
                ]
                # De-duplicate while keeping order.
                expected_pdf_candidates = list(dict.fromkeys(expected_pdf_candidates))
                for cmd in commands_to_try:
                    for convert_target in convert_targets:
                        try:
                            # Rebuild index scenarios may leave old files; only accept files generated by this run.
                            cmd_start_ts = time.time()

                            convert_cmd = [
                                cmd,
                                "--headless",
                                "--convert-to",
                                convert_target,
                                "--outdir",
                                str(temp_path),
                                str(convert_input_path),
                            ]

                            # Prepare conversion subprocess parameters
                            convert_subprocess_kwargs = {
                                "capture_output": True,
                                "text": True,
                                "timeout": 120,  # 60 second timeout
                                "encoding": "utf-8",
                                "errors": "ignore",
                            }

                            # Hide console window on Windows
                            if platform.system() == "Windows":
                                convert_subprocess_kwargs["creationflags"] = (
                                    subprocess.CREATE_NO_WINDOW
                                )

                            result = subprocess.run(convert_cmd, **convert_subprocess_kwargs)

                            # Some PPT files may produce a valid PDF while LibreOffice exits non-zero.
                            # Some filesystems have coarse mtime resolution; keep a small tolerance.
                            fresh_mtime_floor = cmd_start_ts - 2.0
                            fresh_pdf_path = next(
                                (
                                    p
                                    for p in expected_pdf_candidates
                                    if p.exists() and p.stat().st_mtime >= fresh_mtime_floor
                                ),
                                None,
                            )

                            if fresh_pdf_path is not None:
                                conversion_successful = True
                                generated_pdf_path = fresh_pdf_path
                                logger.info(
                                    f"Successfully converted {doc_path.name} to PDF using {cmd} ({convert_target}); generated: {fresh_pdf_path.name}"
                                )
                                break

                            if result.returncode != 0:
                                logger.warning(
                                    f"LibreOffice command '{cmd}' with target '{convert_target}' returned non-zero status without fresh PDF output: {result.stderr}"
                                )
                            else:
                                logger.warning(
                                    f"LibreOffice command '{cmd}' with target '{convert_target}' finished but did not produce fresh expected PDF candidates: {[p.name for p in expected_pdf_candidates]}"
                                )

                        except FileNotFoundError:
                            logger.warning(f"LibreOffice command '{cmd}' not found")
                            break
                        except subprocess.TimeoutExpired:
                            logger.warning(
                                f"LibreOffice command '{cmd}' with target '{convert_target}' timed out"
                            )
                        except Exception as e:
                            logger.error(
                                f"LibreOffice command '{cmd}' with target '{convert_target}' failed with exception: {e}"
                            )

                    if conversion_successful:
                        break

                if not conversion_successful:
                    raise RuntimeError(
                        f"LibreOffice conversion failed for {doc_path.name}. "
                        "Please ensure LibreOffice is installed"
                    )

                if generated_pdf_path is None or not generated_pdf_path.exists():
                    raise RuntimeError(
                        f"PDF conversion failed for {doc_path.name} - expected fresh PDF was not found."
                    )

                pdf_path = generated_pdf_path
                logger.info(f"Generated PDF: {pdf_path.name} ({pdf_path.stat().st_size} bytes)")

                # Copy PDF to final output directory
                output_dir.mkdir(parents=True, exist_ok=True)
                final_pdf_path = output_dir / f"{name_without_suff}.pdf"
                with tempfile.NamedTemporaryFile(
                    dir=output_dir,
                    prefix=f".{name_without_suff}.",
                    suffix=".tmp.pdf",
                    delete=False,
                ) as temp_file:
                    temp_final_pdf_path = Path(temp_file.name)
                try:
                    shutil.copy2(pdf_path, temp_final_pdf_path)
                    temp_final_pdf_path.replace(final_pdf_path)
                except Exception:
                    temp_final_pdf_path.unlink(missing_ok=True)
                    raise

                return final_pdf_path

        except Exception as e:
            logger.error(f"Error in convert_office_to_pdf: {str(e)}")
            raise

    @staticmethod
    def _convert_text_to_pdf(text_path: Path, output_dir: Path) -> Path:
        """
        Convert text file to PDF using ReportLab.

        Args:
            text_path: Path to text file
            output_dir: Output directory

        Returns:
            Path to converted PDF
        """
        try:
            # Read the text content
            text_content, source_encoding = MinerUParser._decode_text_document(text_path)
            if source_encoding.lower() != "utf-8":
                logger.info(f"Successfully read {text_path.name} with {source_encoding} encoding")

            pdf_path = output_dir / f"{text_path.stem}.pdf"

            # Convert text to PDF
            logger.info(f"Converting {text_path.name} to PDF...")

            try:
                from reportlab.lib.pagesizes import A4
                from reportlab.lib.styles import getSampleStyleSheet
                from reportlab.lib.units import inch
                from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

                # Create PDF document
                doc = SimpleDocTemplate(
                    str(pdf_path),
                    pagesize=A4,
                    leftMargin=inch,
                    rightMargin=inch,
                    topMargin=inch,
                    bottomMargin=inch,
                )

                # Get styles
                styles = getSampleStyleSheet()
                normal_style = styles["Normal"]

                # Build content
                story = []

                # Handle markdown or plain text
                if text_path.suffix.lower() == ".md":
                    # Simple markdown handling
                    lines = text_content.split("\n")
                    for line in lines:
                        line = line.strip()
                        if not line:
                            story.append(Spacer(1, 12))
                            continue

                        # Headers
                        if line.startswith("#"):
                            # 计算标题等级，但当前未使用；保留占位便于后续样式扩展
                            _level = len(line) - len(line.lstrip("#"))
                            header_text = line.lstrip("#").strip()
                            if header_text:
                                story.append(Paragraph(header_text, normal_style))
                        else:
                            # Regular text
                            story.append(Paragraph(line, normal_style))
                            story.append(Spacer(1, 6))
                else:
                    # Handle plain text files
                    lines = text_content.split("\n")
                    for line in lines:
                        line = line.rstrip()

                        if not line.strip():
                            story.append(Spacer(1, 6))
                            continue

                        # Escape special characters for ReportLab
                        safe_line = (
                            line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        )

                        # Create paragraph
                        story.append(Paragraph(safe_line, normal_style))
                        story.append(Spacer(1, 3))

                # Build PDF
                doc.build(story)
                logger.info(
                    f"Successfully converted {text_path.name} to PDF ({pdf_path.stat().st_size / 1024:.1f} KB)"
                )

                return pdf_path

            except ImportError:
                raise RuntimeError(
                    "reportlab is required for text-to-PDF conversion. "
                    "Please install it using: pip install reportlab"
                )
            except Exception as e:
                raise RuntimeError(f"Failed to convert text file {text_path.name} to PDF: {str(e)}")

        except Exception as e:
            logger.error(f"Error in convert_text_to_pdf: {str(e)}")
            raise

    def _calculate_file_hash(self, file_path: Optional[Path]) -> str:
        """Calculate SHA256 hash of a file."""
        if not file_path or not file_path.exists():
            logger.warning(f"File {file_path} does not exist")
            return "sha256:unknown"

        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"

    def _extract_title(self, content_items: List[Dict]) -> Optional[str]:
        """Extract document title from content items."""
        for item in content_items[:10]:  # Check first 10 items
            if (
                item.get("type") == "text"
                and item.get("text_level") == 1
                and item.get("text", "").strip()
            ):
                return item["text"].strip()
        return None

    def _detect_language(self, content_items: List[Dict]) -> Optional[str]:
        """Detect document language from text content."""
        text_samples = []
        for item in content_items[:20]:  # Sample first 20 text items
            if item.get("type") == "text" and item.get("text"):
                text_samples.append(item["text"])

        if not text_samples:
            return None

        combined_text = " ".join(text_samples)
        # Simple heuristic: check for Chinese characters
        chinese_chars = sum(1 for char in combined_text if "\u4e00" <= char <= "\u9fff")
        if chinese_chars > len(combined_text) * 0.1:  # 10% threshold
            return "zh"
        return "en"  # Default to English

    def _create_segments_from_content(
        self,
        content_items: List[Dict],
        middle_data: Dict,
        doc_id: str,
        doc_title: str = "",
    ) -> List[Segment]:
        """
        Create Segment objects from content items and middle data.

        Args:
            content_items: List of content items from content_list.json
            middle_data: Data from middle.json for bbox information
            doc_id: Document ID

        Returns:
            List of Segment objects
        """
        segments = []

        def _norm_text(s: str) -> str:
            return " ".join((s or "").strip().split())

        def _loc_from_index(idx: int) -> Optional[Dict[str, Any]]:
            if idx < 0 or idx >= len(content_items):
                return None
            it = content_items[idx]
            page = it.get("page_idx")
            bb = middle_bboxes[idx] or it.get("bbox")
            if page is None or not bb:
                return None
            return {"page": int(page), "bbox": bb}

        def _best_match_text_index(
            *,
            anchor_index: int,
            anchor_bbox: Optional[List[float]],
            anchor_page_idx: Optional[int],
            target_text: str,
            window: int,
        ) -> Optional[int]:
            """Find best matching nearby text item index (same page) for target_text."""
            if not target_text:
                return None
            if anchor_bbox is None or anchor_page_idx is None:
                return None

            target_norm = _norm_text(target_text)
            if not target_norm:
                return None

            # Search in a local neighborhood around the anchor.
            start = max(0, anchor_index - window)
            end = min(len(content_items), anchor_index + window + 1)
            candidates: List[Tuple[float, int]] = []
            for j in range(start, end):
                if j == anchor_index:
                    continue
                cand = content_items[j]
                if cand.get("type") != "text":
                    continue
                if cand.get("page_idx") != anchor_page_idx:
                    continue
                cand_text = _norm_text(str(cand.get("text", "")))
                if not cand_text:
                    continue
                # Prefer exact match; allow containment only when short enough.
                exact = cand_text == target_norm
                contain = (target_norm in cand_text or cand_text in target_norm) and min(
                    len(cand_text), len(target_norm)
                ) <= 180
                if not (exact or contain):
                    continue
                cand_bbox = middle_bboxes[j] or cand.get("bbox")
                if not cand_bbox:
                    continue
                score = self._caption_candidate_score(
                    fig_bbox=anchor_bbox,
                    cand_bbox=cand_bbox,
                    cand_text=cand_text,
                )
                # Slightly penalize non-exact matches.
                if not exact:
                    score *= 1.25
                candidates.append((float(score), j))
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0])
            return int(candidates[0][1])

        # Pre-compute middle.json bboxes for all items.
        # Caption recovery may look ahead/behind, so we cannot rely on a single-pass
        # cursor when we need candidate bboxes.
        middle_bboxes: List[Optional[List[float]]] = [None] * len(content_items)
        middle_para_blocks: List[Optional[Dict[str, Any]]] = [None] * len(content_items)
        para_block_cursor_by_page: Dict[int, int] = {}
        discarded_block_cursor_by_page: Dict[int, int] = {}
        for idx, it in enumerate(content_items):
            page_idx = it.get("page_idx")
            if page_idx is None:
                middle_bboxes[idx] = None
                middle_para_blocks[idx] = None
                continue

            item_type = str(it.get("type", "")).strip().lower()
            page_int = int(page_idx)

            # MinerU stores header/footer/page number in discarded_blocks instead of para_blocks.
            if item_type in {"header", "footer", "page_number", "aside_text", "discarded"}:
                discarded_index = discarded_block_cursor_by_page.get(page_int, 0)
                middle_bboxes[idx] = self._extract_discarded_bbox_from_middle(
                    discarded_index, it, middle_data
                )
                discarded_block_cursor_by_page[page_int] = discarded_index + 1
                middle_para_blocks[idx] = None
            else:
                para_index = para_block_cursor_by_page.get(page_int, 0)
                middle_bboxes[idx] = self._extract_bbox_from_middle(para_index, it, middle_data)
                para_block_cursor_by_page[page_int] = para_index + 1

                # Keep the full para_block for caption/footnote bbox extraction.
                try:
                    page_data = middle_data.get("pdf_info", [])[page_int]
                    para_blocks = page_data.get("para_blocks", [])
                    middle_para_blocks[idx] = (
                        para_blocks[para_index] if para_index < len(para_blocks) else None
                    )
                except Exception:
                    middle_para_blocks[idx] = None

        def _extract_subblock_locations(
            *,
            para_block: Optional[Dict[str, Any]],
            page_idx: Optional[int],
            wanted_types: Tuple[str, ...],
        ) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
            if not para_block or page_idx is None:
                return None
            blocks = para_block.get("blocks") or []
            hits: List[Dict[str, Any]] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = str(b.get("type") or "")
                if bt not in wanted_types:
                    continue
                bb = b.get("bbox")
                if not bb:
                    continue
                hits.append({"page": int(page_idx), "bbox": bb, "block_type": bt})
            if not hits:
                return None
            if len(hits) == 1:
                # Keep output minimal for frontend; still include block_type for debugging.
                one = dict(hits[0])
                one.pop("block_type", None)
                return one
            # Multiple caption/footnote blocks (rare): keep as list.
            for h in hits:
                h.pop("block_type", None)
            return hits

        current_page = 0
        current_content_page_index = 0

        used_caption_text_indices: set = set()
        max_distance = self.DEFAULT_MAX_CAPTION_DISTANCE

        for i, item in enumerate(content_items):
            segment_id = f"{doc_id}_seg_{i:04d}"
            item_type = item.get("type", "text")

            # Track page changes for bbox indexing
            page_idx = item.get("page_idx")
            if page_idx is not None:
                if page_idx > current_page:
                    current_page = page_idx
                    current_content_page_index = 0
                else:
                    current_content_page_index += 1

            # Extract bbox information from middle.json (precomputed)
            bbox = middle_bboxes[i]

            if item_type == "text":
                segment = self._create_text_segment(item, segment_id, doc_id, i, bbox)
            elif item_type == "image":
                # Skip image segments without image path
                img_path = item.get("img_path", "")
                if not img_path:
                    logger.warning(
                        f"Skipping image segment {segment_id} in document {doc_id} without img_path."
                    )
                    continue

                para_title = self._find_prev_level1_title(content_items, i)

                captions = item.get("image_caption", item.get("img_caption", []))
                caption = " ".join(captions) if isinstance(captions, list) else str(captions)
                caption = (caption or "").strip()

                # Recover missing caption from nearby text blocks
                debug_payload: Optional[dict] = None
                caption_best_idx: Optional[int] = None
                if not caption:
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    if current_bbox and current_page_idx is not None:
                        candidate_indices = self._find_nearest_text_candidates(
                            content_items,
                            i,
                            used_caption_text_indices,
                            window=self.DEFAULT_CAPTION_CANDIDATE_WINDOW,
                            max_candidates=self.DEFAULT_CAPTION_CANDIDATE_WINDOW,
                        )
                        scored: List[Tuple[float, int, Optional[Dict[str, float]]]] = []
                        if self.debug_caption_recovery:
                            debug_payload = {
                                "type": "image",
                                "item_index": i,
                                "max_distance": float(max_distance),
                                "candidate_window": int(self.DEFAULT_CAPTION_CANDIDATE_WINDOW),
                                "candidates_top2": [],
                            }
                        for ci in candidate_indices:
                            cand = content_items[ci]
                            if cand.get("page_idx") != current_page_idx:
                                continue
                            cand_bbox = middle_bboxes[ci] or cand.get("bbox")
                            if not cand_bbox:
                                continue
                            dist = self._rect_collision_distance(current_bbox, cand_bbox)
                            if dist <= max_distance:
                                cand_text = str(cand.get("text", "")).strip()
                                if not cand_text:
                                    continue
                                if self.debug_caption_recovery:
                                    score, comp = self._caption_candidate_components(
                                        fig_bbox=current_bbox,
                                        cand_bbox=cand_bbox,
                                        cand_text=cand_text,
                                    )
                                    scored.append((score, ci, comp))
                                else:
                                    score = self._caption_candidate_score(
                                        fig_bbox=current_bbox,
                                        cand_bbox=cand_bbox,
                                        cand_text=cand_text,
                                    )
                                    scored.append((score, ci, None))

                        if scored:
                            scored.sort(key=lambda x: x[0])
                            _, best_idx, _ = scored[0]
                            caption_best_idx = int(best_idx)
                            caption = str(content_items[best_idx].get("text", "")).strip()
                            if caption:
                                used_caption_text_indices.add(best_idx)

                            if debug_payload is not None:
                                top2 = []
                                for score, ci, comp in scored[:2]:
                                    text = str(content_items[ci].get("text", "")).strip()
                                    top2.append(
                                        {
                                            "content_index": int(ci),
                                            "score": float(score),
                                            "text": text[:200],
                                            "effective_dist": float(
                                                (comp or {}).get("effective_dist", 0.0)
                                            ),
                                            "align_factor": float(
                                                (comp or {}).get("align_factor", 0.0)
                                            ),
                                            "size_factor": float(
                                                (comp or {}).get("size_factor", 0.0)
                                            ),
                                            "newline_factor": float(
                                                (comp or {}).get("newline_factor", 0.0)
                                            ),
                                            "length_factor": float(
                                                (comp or {}).get("length_factor", 0.0)
                                            ),
                                        }
                                    )
                                debug_payload["candidates_top2"] = top2
                                debug_payload["selected_index"] = int(best_idx)
                                debug_payload["selected_score"] = float(scored[0][0])
                        elif debug_payload is not None:
                            debug_payload["candidates_top2"] = []

                # If caption comes from MinerU field, try to locate its bbox by matching nearby text.
                if caption and caption_best_idx is None:
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    caption_best_idx = _best_match_text_index(
                        anchor_index=i,
                        anchor_bbox=current_bbox,
                        anchor_page_idx=current_page_idx,
                        target_text=caption,
                        window=max(8, self.DEFAULT_CAPTION_CANDIDATE_WINDOW),
                    )

                caption_text = "".join(
                    [
                        f"[doc_title]{doc_title or ''}",
                        f"[para_title]{para_title or ''}",
                        f"[caption]{caption or ''}",
                    ]
                )

                segment = self._create_image_segment(
                    item,
                    segment_id,
                    doc_id,
                    i,
                    bbox,
                    doc_title=doc_title or "",
                    para_title=para_title or "",
                    caption=caption,
                    caption_text=caption_text,
                )

                # Caption/footnote bbox: prefer middle.json sub-blocks for 100% layout consistency.
                page_idx_i = item.get("page_idx")
                para_block = middle_para_blocks[i]
                cap_loc = _extract_subblock_locations(
                    para_block=para_block,
                    page_idx=page_idx_i,
                    wanted_types=("image_caption",),
                )
                foot_loc = _extract_subblock_locations(
                    para_block=para_block,
                    page_idx=page_idx_i,
                    wanted_types=("image_footnote",),
                )

                # Fallback: when MinerU didn't emit caption/footnote blocks, try text matching.
                if not cap_loc:
                    cap_loc2 = (
                        _loc_from_index(caption_best_idx) if caption_best_idx is not None else None
                    )
                    if cap_loc2:
                        cap_loc = cap_loc2
                if not foot_loc:
                    footnotes_raw = item.get("image_footnote", item.get("img_footnote", []))
                    footnote_candidates: List[str] = []
                    if isinstance(footnotes_raw, list):
                        footnote_candidates = [
                            str(x).strip() for x in footnotes_raw if str(x).strip()
                        ]
                    elif footnotes_raw:
                        footnote_candidates = [str(footnotes_raw).strip()]

                    foot_best_idx: Optional[int] = None
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    for ft in footnote_candidates:
                        mi = _best_match_text_index(
                            anchor_index=i,
                            anchor_bbox=current_bbox,
                            anchor_page_idx=current_page_idx,
                            target_text=ft,
                            window=max(10, self.DEFAULT_CAPTION_CANDIDATE_WINDOW * 2),
                        )
                        if mi is not None:
                            foot_best_idx = mi
                            break
                    ft_loc2 = _loc_from_index(foot_best_idx) if foot_best_idx is not None else None
                    if ft_loc2:
                        foot_loc = ft_loc2

                if cap_loc:
                    segment.extra["caption_location"] = cap_loc
                if foot_loc:
                    segment.extra["footnote_location"] = foot_loc

                if debug_payload is not None:
                    segment.extra["caption_recovery_debug"] = debug_payload
            elif item_type == "table":
                # Skip table segments without image path
                img_path = item.get("img_path", "")
                if not img_path:
                    logger.warning(
                        f"Skipping table segment {segment_id} in document {doc_id} without img_path."
                    )
                    continue
                para_title = self._find_prev_level1_title(content_items, i)

                captions = item.get("table_caption", [])
                caption = " ".join(captions) if isinstance(captions, list) else str(captions)
                caption = (caption or "").strip()

                debug_payload: Optional[dict] = None
                caption_best_idx: Optional[int] = None
                if not caption:
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    if current_bbox and current_page_idx is not None:
                        candidate_indices = self._find_nearest_text_candidates(
                            content_items,
                            i,
                            used_caption_text_indices,
                            window=self.DEFAULT_CAPTION_CANDIDATE_WINDOW,
                            max_candidates=self.DEFAULT_CAPTION_CANDIDATE_WINDOW,
                        )
                        scored: List[Tuple[float, int, Optional[Dict[str, float]]]] = []
                        if self.debug_caption_recovery:
                            debug_payload = {
                                "type": "table",
                                "item_index": i,
                                "max_distance": float(max_distance),
                                "candidate_window": int(self.DEFAULT_CAPTION_CANDIDATE_WINDOW),
                                "candidates_top2": [],
                            }
                        for ci in candidate_indices:
                            cand = content_items[ci]
                            if cand.get("page_idx") != current_page_idx:
                                continue
                            cand_bbox = middle_bboxes[ci] or cand.get("bbox")
                            if not cand_bbox:
                                continue
                            dist = self._rect_collision_distance(current_bbox, cand_bbox)
                            if dist <= max_distance:
                                cand_text = str(cand.get("text", "")).strip()
                                if not cand_text:
                                    continue
                                if self.debug_caption_recovery:
                                    score, comp = self._caption_candidate_components(
                                        fig_bbox=current_bbox,
                                        cand_bbox=cand_bbox,
                                        cand_text=cand_text,
                                    )
                                    scored.append((score, ci, comp))
                                else:
                                    score = self._caption_candidate_score(
                                        fig_bbox=current_bbox,
                                        cand_bbox=cand_bbox,
                                        cand_text=cand_text,
                                    )
                                    scored.append((score, ci, None))

                        if scored:
                            scored.sort(key=lambda x: x[0])
                            _, best_idx, _ = scored[0]
                            caption_best_idx = int(best_idx)
                            caption = str(content_items[best_idx].get("text", "")).strip()
                            if caption:
                                used_caption_text_indices.add(best_idx)

                            if debug_payload is not None:
                                top2 = []
                                for score, ci, comp in scored[:2]:
                                    text = str(content_items[ci].get("text", "")).strip()
                                    top2.append(
                                        {
                                            "content_index": int(ci),
                                            "score": float(score),
                                            "text": text[:200],
                                            "effective_dist": float(
                                                (comp or {}).get("effective_dist", 0.0)
                                            ),
                                            "align_factor": float(
                                                (comp or {}).get("align_factor", 0.0)
                                            ),
                                            "size_factor": float(
                                                (comp or {}).get("size_factor", 0.0)
                                            ),
                                            "newline_factor": float(
                                                (comp or {}).get("newline_factor", 0.0)
                                            ),
                                            "length_factor": float(
                                                (comp or {}).get("length_factor", 0.0)
                                            ),
                                        }
                                    )
                                debug_payload["candidates_top2"] = top2
                                debug_payload["selected_index"] = int(best_idx)
                                debug_payload["selected_score"] = float(scored[0][0])
                        elif debug_payload is not None:
                            debug_payload["candidates_top2"] = []

                # If caption comes from MinerU field, try to locate its bbox by matching nearby text.
                if caption and caption_best_idx is None:
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    caption_best_idx = _best_match_text_index(
                        anchor_index=i,
                        anchor_bbox=current_bbox,
                        anchor_page_idx=current_page_idx,
                        target_text=caption,
                        window=max(8, self.DEFAULT_CAPTION_CANDIDATE_WINDOW),
                    )

                caption_text = "".join(
                    [
                        f"[doc_title]{doc_title or ''}",
                        f"[para_title]{para_title or ''}",
                        f"[caption]{caption or ''}",
                    ]
                )

                segment = self._create_table_segment(
                    item,
                    segment_id,
                    doc_id,
                    i,
                    bbox,
                    doc_title=doc_title or "",
                    para_title=para_title or "",
                    caption=caption,
                    caption_text=caption_text,
                )

                page_idx_i = item.get("page_idx")
                para_block = middle_para_blocks[i]
                cap_loc = _extract_subblock_locations(
                    para_block=para_block,
                    page_idx=page_idx_i,
                    wanted_types=("table_caption",),
                )
                foot_loc = _extract_subblock_locations(
                    para_block=para_block,
                    page_idx=page_idx_i,
                    wanted_types=("table_footnote",),
                )

                if not cap_loc:
                    cap_loc2 = (
                        _loc_from_index(caption_best_idx) if caption_best_idx is not None else None
                    )
                    if cap_loc2:
                        cap_loc = cap_loc2

                if not foot_loc:
                    footnotes_raw = item.get("table_footnote", [])
                    footnote_candidates: List[str] = []
                    if isinstance(footnotes_raw, list):
                        footnote_candidates = [
                            str(x).strip() for x in footnotes_raw if str(x).strip()
                        ]
                    elif footnotes_raw:
                        footnote_candidates = [str(footnotes_raw).strip()]

                    foot_best_idx: Optional[int] = None
                    current_bbox = bbox or item.get("bbox")
                    current_page_idx = item.get("page_idx")
                    for ft in footnote_candidates:
                        mi = _best_match_text_index(
                            anchor_index=i,
                            anchor_bbox=current_bbox,
                            anchor_page_idx=current_page_idx,
                            target_text=ft,
                            window=max(10, self.DEFAULT_CAPTION_CANDIDATE_WINDOW * 2),
                        )
                        if mi is not None:
                            foot_best_idx = mi
                            break
                    ft_loc2 = _loc_from_index(foot_best_idx) if foot_best_idx is not None else None
                    if ft_loc2:
                        foot_loc = ft_loc2

                if cap_loc:
                    segment.extra["caption_location"] = cap_loc
                if foot_loc:
                    segment.extra["footnote_location"] = foot_loc

                if debug_payload is not None:
                    segment.extra["caption_recovery_debug"] = debug_payload
            elif item_type == "list":
                segment = self._create_list_segment(item, segment_id, doc_id, i, bbox)
            elif item_type == "code":
                segment = self._create_code_segment(item, segment_id, doc_id, i, bbox)
            elif item_type == "equation":
                segment = self._create_equation_segment(item, segment_id, doc_id, i, bbox)
            elif item_type == "discarded":
                # Skip discarded items
                continue
            else:
                # Handle unknown types as text
                location = {"page": item.get("page_idx", 0)}
                if bbox:
                    location["bbox"] = bbox

                segment = Segment(
                    segment_id=segment_id,
                    doc_id=doc_id,
                    order=i,
                    type="text",
                    text=str(item.get("text", "")),
                    location=location,
                    extra={
                        "original_type": item_type,
                        "source": "minerU",
                        "raw_item": item,
                    },
                )

            segments.append(segment)

        return self._normalize_parser_noise_segments(segments)

    def _extract_bbox_from_middle(
        self, content_index: int, content_item: Dict, middle_data: Dict
    ) -> Optional[List[float]]:
        """
        Extract bbox information from middle.json data for a given content item.

        Args:
            content_index: Index within the current page's content
            content_item: Item from content_list.json
            middle_data: Loaded middle.json data

        Returns:
            Bbox coordinates [x0, y0, x1, y1] or None if not found
        """

        def _extract_bbox_from_content_item(item: Dict[str, Any]) -> Optional[List[float]]:
            bbox = item.get("bbox")
            if bbox:
                return bbox

            extra = item.get("extra") or {}
            if isinstance(extra, dict):
                bbox = extra.get("bbox")
                if bbox:
                    return bbox

                location = extra.get("location")
                if isinstance(location, dict):
                    bbox = location.get("bbox")
                    if bbox:
                        return bbox

            return None

        try:
            page_idx = content_item.get("page_idx", 0)

            # Get the page data from middle.json
            if page_idx >= len(middle_data.get("pdf_info", [])):
                return _extract_bbox_from_content_item(content_item)

            page_data = middle_data["pdf_info"][page_idx]
            para_blocks = page_data.get("para_blocks", [])

            if len(para_blocks) > content_index:
                block = para_blocks[content_index]
                bbox = block.get("bbox")
                if bbox:
                    return bbox

            # middle.json 对应块没有 bbox 时，回退到 content_list 原始条目中的 bbox。
            return _extract_bbox_from_content_item(content_item)
        except Exception as e:
            logger.debug(f"Error extracting bbox: {e}")
            return _extract_bbox_from_content_item(content_item)

    def _extract_discarded_bbox_from_middle(
        self, discarded_index: int, content_item: Dict, middle_data: Dict
    ) -> Optional[List[float]]:
        """Extract bbox for header/footer/page_number from middle.json discarded_blocks."""

        def _extract_bbox_from_content_item(item: Dict[str, Any]) -> Optional[List[float]]:
            bbox = item.get("bbox")
            if bbox:
                return bbox

            extra = item.get("extra") or {}
            if isinstance(extra, dict):
                bbox = extra.get("bbox")
                if bbox:
                    return bbox

                location = extra.get("location")
                if isinstance(location, dict):
                    bbox = location.get("bbox")
                    if bbox:
                        return bbox

            return None

        def _normalize_text(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        try:
            page_idx = content_item.get("page_idx", 0)
            if page_idx >= len(middle_data.get("pdf_info", [])):
                return _extract_bbox_from_content_item(content_item)

            page_data = middle_data["pdf_info"][page_idx]
            discarded_blocks = page_data.get("discarded_blocks", [])

            if 0 <= discarded_index < len(discarded_blocks):
                block = discarded_blocks[discarded_index]
                bbox = block.get("bbox")
                if bbox:
                    return bbox

            # Fallback: match by text against discarded span content on the same page.
            target_text = _normalize_text(content_item.get("text", ""))
            if target_text:
                for block in discarded_blocks:
                    for line in block.get("lines", []) or []:
                        for span in line.get("spans", []) or []:
                            span_text = _normalize_text(span.get("content", ""))
                            if not span_text:
                                continue
                            if (
                                span_text == target_text
                                or target_text in span_text
                                or span_text in target_text
                            ):
                                return span.get("bbox") or line.get("bbox") or block.get("bbox")

            return _extract_bbox_from_content_item(content_item)
        except Exception as e:
            logger.debug(f"Error extracting discarded bbox: {e}")
            return _extract_bbox_from_content_item(content_item)

    def _create_text_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
    ) -> Segment:
        """Create a text segment from content_list item."""
        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="text",
            text=item.get("text", ""),
            location=location,
            extra={
                "text_level": item.get("text_level"),
                "source": "minerU",
                "original_type": item.get("type", "text"),
            },
        )

    def _create_list_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
    ) -> Segment:
        """Create a list segment from content_list item."""
        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        list_items = item.get("list_items", [])
        if isinstance(list_items, list):
            text = "\n".join(str(x).strip() for x in list_items if str(x).strip())
        else:
            text = str(list_items).strip()

        if not text:
            text = str(item.get("text", ""))

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="text",
            text=text,
            location=location,
            extra={
                "sub_type": item.get("sub_type"),
                "list_items": list_items,
                "source": "minerU",
                "original_type": item.get("type", "list"),
            },
        )

    def _create_code_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
    ) -> Segment:
        """Create a code segment from content_list item."""
        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        code_text = self._extract_mineru_inline_text(item.get("code_body"))
        if not code_text:
            code_text = self._extract_mineru_inline_text(item.get("text"))
        if not code_text:
            code_text = self._extract_mineru_inline_text(item.get("code"))
        if not code_text and isinstance(content, dict):
            code_text = self._extract_mineru_inline_text(content.get("code_content"))

        caption_text = self._extract_mineru_inline_text(item.get("code_caption"))
        if not caption_text and isinstance(content, dict):
            caption_text = self._extract_mineru_inline_text(content.get("code_caption"))

        code_language = str(
            item.get("guess_lang")
            or item.get("code_language")
            or (content.get("code_language") if isinstance(content, dict) else "")
            or ""
        ).strip()

        segment_text = code_text
        if code_text:
            fence = f"```{code_language}" if code_language else "```"
            segment_text = f"{fence}\n{code_text.rstrip()}\n```"
        if caption_text:
            segment_text = f"{caption_text.strip()}\n\n{segment_text}".strip()

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="text",
            text=segment_text,
            location=location,
            extra={
                "source": "minerU",
                "original_type": item.get("type", "code"),
                "code_language": code_language,
                "code_caption": caption_text,
                "code_body": code_text,
            },
        )

    @staticmethod
    def _extract_mineru_inline_text(value: Any) -> str:
        """Extract text from MinerU scalar/list inline content values."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts: List[str] = []
            for entry in value:
                if isinstance(entry, dict):
                    text = entry.get("content")
                    if text is None:
                        text = entry.get("text")
                    if text is not None:
                        parts.append(str(text))
                elif entry is not None:
                    parts.append(str(entry))
            return "".join(parts).strip()
        if isinstance(value, dict):
            text = value.get("content")
            if text is None:
                text = value.get("text")
            return str(text).strip() if text is not None else ""
        return str(value).strip()

    def _create_image_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
        *,
        doc_title: str = "",
        para_title: str = "",
        caption: str = "",
        caption_text: str = "",
    ) -> Segment:
        """Create an image segment from content_list item."""
        # Extract footnote text
        footnotes = item.get("image_footnote", item.get("img_footnote", []))

        footnote_text = " ".join(footnotes) if isinstance(footnotes, list) else str(footnotes)
        base_caption_text = (caption_text or "").strip()
        combined_text = f"{base_caption_text} {footnote_text}".strip()

        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="image",
            text=combined_text,
            location=location,
            extra={
                "img_path": item.get("img_path", ""),
                "caption": base_caption_text,
                "footnote": footnote_text,
                "source": "minerU",
            },
        )

    def _create_table_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
        *,
        doc_title: str = "",
        para_title: str = "",
        caption: str = "",
        caption_text: str = "",
    ) -> Segment:
        """Create a table segment from content_list item."""
        # Extract table text content
        table_text = ""

        # Try to get table body text
        table_body = item.get("table_body", "")
        # if table_body and isinstance(table_body, str):
        #     # For HTML table content, extract text (simplified)
        #     import re
        #     table_text = re.sub(r'<[^>]+>', ' ', table_body).strip()
        #     table_text = ' '.join(table_text.split())

        # Add footnotes
        footnotes = item.get("table_footnote", [])

        footnote_text = " ".join(footnotes) if isinstance(footnotes, list) else str(footnotes)
        base_caption_text = (caption_text or "").strip()

        combined_text = f"{base_caption_text} {table_text} {footnote_text}".strip()

        img_path = item.get("img_path", "")

        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="table",
            text=combined_text,
            location=location,
            extra={
                "img_path": img_path,
                "table_body": table_body,
                "caption": base_caption_text,
                "footnote": footnote_text,
                "source": "minerU",
            },
        )

    def _create_equation_segment(
        self,
        item: Dict,
        segment_id: str,
        doc_id: str,
        order: int,
        bbox: Optional[List[float]] = None,
    ) -> Segment:
        """Create an equation segment from content_list item."""
        # Equation text might be in LaTeX format or plain text
        equation_text = item.get("text", "")
        text_format = item.get("text_format", "")

        location = {"page": item.get("page_idx", 0)}

        if bbox:
            location["bbox"] = bbox

        return Segment(
            segment_id=segment_id,
            doc_id=doc_id,
            order=order,
            type="text",  # Treat equations as text for now
            text=equation_text,
            location=location,
            extra={
                "text_format": text_format,
                "img_path": item.get("img_path", ""),
                "equation": True,
                "source": "minerU",
            },
        )

    def validate_parsing(self, asset: Asset, segments: List[Segment]) -> bool:
        """
        Validate the parsing results for consistency.

        Args:
            asset: Parsed Asset object
            segments: List of parsed Segment objects

        Returns:
            True if validation passes
        """
        # Check segments count matches
        if asset.segments_count != len(segments):
            return False

        # Check all segments have same doc_id
        for segment in segments:
            if segment.doc_id != asset.doc_id:
                return False

        # Segment order is the source content-list index; filtering parser noise leaves gaps.
        orders = [seg.order for seg in segments]
        if orders != sorted(set(orders)):
            return False

        # Check segment_id uniqueness
        segment_ids = [seg.segment_id for seg in segments]
        if len(set(segment_ids)) != len(segment_ids):
            return False

        return True

    def check_installation(self) -> bool:
        """
        Check if MinerU is properly installed.

        Returns:
            True if installation is valid, False otherwise
        """
        try:
            subprocess_kwargs = {
                "capture_output": True,
                "text": True,
                "check": True,
                "encoding": "utf-8",
                "errors": "ignore",
            }

            # Hide console window on Windows
            if platform.system() == "Windows":
                subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(["mineru", "--version"], **subprocess_kwargs)
            logger.debug(f"MinerU version: {result.stdout.strip()}")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.debug(
                "MinerU is not properly installed. "
                "Please install it using: pip install -U 'mineru[pipeline,vlm]'"
            )
            return False
