"""
默认任务处理器

保留以下默认处理器：
- DocParser
- VideoParser
- KGIndexer
- VideoEmbedder
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from hyperrag.asset_status import AssetStatus, get_asset_status_storage
from hyperrag.kg.constants import PRECOMPILED_WORKSPACE

from ..parser import MinerUParser
from ..schema import Asset, Segment
from ..utils.job_log import setup_logger, with_job_log
from ..utils.segment_tools import (
    build_video_segments,
    get_primary_video_source,
)
from .models import TaskResult

logger = logging.getLogger(__name__)


def _mint_storage_download_url(library_id: str, asset_id: str) -> str | None:
    """Mint a grant URL via viknow-storage (24h TTL). Used when job params omit source_download_url."""
    import os

    base = os.environ.get("VIKNOW_STORAGE_URL", "").strip().rstrip("/")
    token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
    if not base or not token or not library_id or not asset_id:
        return None
    try:
        import httpx

        response = httpx.post(
            f"{base}/libraries/{library_id}/assets/{asset_id}/download-url",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=60.0,
        )
        if response.status_code >= 400:
            logger.warning(
                "storage download-url mint failed: status=%s library=%s asset=%s",
                response.status_code,
                library_id,
                asset_id,
            )
            return None
        payload = response.json()
        url = str(payload.get("url") or "").strip()
        return url or None
    except Exception:
        logger.exception("storage download-url mint error library=%s asset=%s", library_id, asset_id)
        return None


def _resolve_video_parse_source(
    asset_path: object,
    metadata: dict[str, Any],
    nested_params: dict[str, Any],
    *,
    workspace: str,
    asset_id: str,
) -> str:
    for key in ("source_download_url", "source_media_url"):
        for bag in (metadata, nested_params):
            value = bag.get(key) if isinstance(bag, dict) else None
            if value is not None and str(value).strip():
                return str(value).strip()
    text = str(asset_path or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("storage://"):
        url = _mint_storage_download_url(workspace, asset_id)
        if url:
            return url
    return text

try:
    from actio.video_parser import VideoParser as ActioVideoParser
    from actio.video_parser import VideoParserConfig as ActioVideoParserConfig
except ModuleNotFoundError:  # pragma: no cover - optional dependency in standalone HyperRAG tests
    ActioVideoParser = None
    ActioVideoParserConfig = None

if TYPE_CHECKING:
    from hyperrag.artifact_store import ArtifactStore
    from hyperrag.bm25_storage_manager import BM25StorageManager

    from .models import Task


setup_logger()


def _emit_progress(message: str) -> None:
    logger.info(message)
    print(message, flush=True)


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coerce_video_duration(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _extract_video_duration(*payloads: Any) -> float | None:
    """Extract the source video duration from parser output before it is discarded."""
    direct_keys = ("video_duration", "duration_sec", "duration_seconds", "duration")
    for payload in payloads:
        if not isinstance(payload, dict):
            continue

        for key in direct_keys:
            duration = _coerce_video_duration(payload.get(key))
            if duration is not None:
                return duration

        video_info = payload.get("video_info")
        if isinstance(video_info, dict):
            duration = _coerce_video_duration(video_info.get("duration_sec"))
            if duration is not None:
                return duration

        metadata = payload.get("metadata")
        metadata_items = metadata if isinstance(metadata, list) else [metadata]
        for item in metadata_items:
            if not isinstance(item, dict):
                continue
            for key in direct_keys:
                duration = _coerce_video_duration(item.get(key))
                if duration is not None:
                    return duration
            video_info = item.get("video_info")
            if isinstance(video_info, dict):
                duration = _coerce_video_duration(video_info.get("duration_sec"))
                if duration is not None:
                    return duration

        nodes = payload.get("nodes")
        if isinstance(nodes, list):
            max_end = 0.0
            for group in nodes:
                group_items = group if isinstance(group, list) else [group]
                for node in group_items:
                    if isinstance(node, dict):
                        end = _coerce_video_duration(node.get("end"))
                        if end is not None:
                            max_end = max(max_end, end)
            if max_end > 0:
                return max_end

    return None


def _attach_video_chunk_ids(
    segments: Any,
    segment_chunk_ids: Any,
) -> list[dict[str, Any]]:
    if not isinstance(segments, list):
        return []
    if not isinstance(segment_chunk_ids, dict):
        return [segment for segment in segments if isinstance(segment, dict)]

    enriched_segments: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_copy = dict(segment)
        segment_id = str(segment_copy.get("segment_id") or "").strip()
        chunk_id = str(segment_chunk_ids.get(segment_id) or "").strip()
        if chunk_id:
            extra = segment_copy.get("extra")
            segment_extra = dict(extra) if isinstance(extra, dict) else {}
            segment_extra["chunk_id"] = chunk_id
            segment_copy["extra"] = segment_extra
        enriched_segments.append(segment_copy)
    return enriched_segments


class BaseTaskHandler(ABC):
    """
    任务处理器基类

    所有 handler 应继承此类，实现 __call__ 方法。
    依赖通过构造函数注入。
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "__call__" in cls.__dict__:
            cls.__call__ = with_job_log(lambda self, task: task.job_id)(cls.__call__)

    @abstractmethod
    async def __call__(self, task: "Task") -> TaskResult:
        """
        执行任务

        Args:
            task: 任务实例

        Returns:
            任务执行结果
        """
        pass


# ============== 文档解析 ==============
class DocParserHandler(BaseTaskHandler):
    """文档解析处理器：调用 MinerUParser 生成 asset 和 segments。"""

    def __init__(self, artifact_store: "ArtifactStore", asr_model_func=None):
        """
        初始化文档解析处理器

        Args:
            artifact_store: Artifact存储管理器
            asr_model_func: ASR 模型函数参数
        """
        self.artifact_store = artifact_store
        self.asr_model_func = asr_model_func

    async def __call__(self, task: "Task") -> TaskResult:
        md = task.metadata if isinstance(task.metadata, dict) else {}
        asset_path = md.get("asset_path")
        # 兼容两种 task.metadata 结构：
        # - v1 旧结构：{"asset_path": ..., "params": {...}}
        # - v2 新结构：{"asset_path": ..., **params}
        params = md.get("params") if isinstance(md.get("params"), dict) else md
        if isinstance(params, dict):
            params = dict(params)
            params.pop("asset_path", None)

        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        try:
            mineru_parser = MinerUParser(asr_model_func=self.asr_model_func)
            # 从 ArtifactStore 获取 mineru cache 根目录
            cache_dir = self.artifact_store.get_mineru_cache_root(task.workspace)

            language = params.get("language", "zh")
            title = params.get("title")
            asset, segments = await asyncio.to_thread(
                mineru_parser.parse_document,
                task.asset_id,
                Path(asset_path),
                cache_dir,
                dict(
                    method=params.get("method", "auto"),
                    lang="ch" if language == "zh" else "en",
                    device=params.get("device"),
                    backend=params.get("backend"),
                    formula=params.get("formula", True),
                    table=params.get("table", True),
                    title=title,
                    ppt_backbone=(params.get("attributes") or {}).get("ppt_backbone", "visual"),
                    asr_hotwords=params.get("asr_hotwords", []),
                    ppt_media_asr_enabled=params.get("ppt_media_asr_enabled", True),
                    ppt_notes_export_for_mineru=params.get("ppt_notes_export_for_mineru", True),
                ),
            )

            return TaskResult(
                status="completed",
                data={
                    "asset_path": asset_path,
                    "asset": _safe_model_dump(asset),
                    "segments": [_safe_model_dump(s) for s in segments],
                },
            )
        except Exception as e:
            logger.exception(f"DocParser failed: {e}")
            return TaskResult(status="failed", error=str(e))


# ============== 视频解析 ==============
# async def video_parser_handler(task: "Task", context) -> TaskResult:
#     """
#     视频解析：调用 VideoParser 生成 asset 和 segments。
#     """
#     asset_path = task.metadata.get("asset_path")
#     params = task.metadata.get("params", {}) if isinstance(task.metadata, dict) else {}

#     if not asset_path:
#         return TaskResult(status="failed", error="asset_path not found in metadata")

#     try:
#         parser_params = dict(
#             segment_length_sec=params.get("segment_length_sec", 30),
#             clip_sample_fps=params.get("clip_sample_fps", 1.0),
#             asr_lang=params.get("language", "zh"),
#             asr_hotwords=params.get("asr_hotwords", []),
#         )

#         base_cache_dir = getattr(context, "video_cache_dir", None)
#         video_parser = VideoParser(cache_root_dir=base_cache_dir)
#         asset, segments = await asyncio.to_thread(
#             video_parser.parse_document,
#             task.asset_id,
#             Path(asset_path),
#             parser_params,
#         )

#         content = _build_index_content(asset, segments)

#         return TaskResult(
#             status="completed",
#             data={
#                 "content": content,
#                 "asset_path": asset_path,
#                 "asset": _safe_model_dump(asset),
#                 "segments": [_safe_model_dump(s) for s in segments],
#             },
#         )
#     except Exception as e:
#         logger.exception(f"VideoParser failed: {e}")
#         return TaskResult(status="failed", error=str(e))


# ============== KG 索引 ==============
class KGIndexHandler(BaseTaskHandler):
    """Single-layer KG 索引处理器。"""

    def __init__(self, hyperrag_v2: Any):
        self.hyperrag_v2 = hyperrag_v2

    async def __call__(self, task: "Task") -> TaskResult:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        asset_path = str(metadata.get("asset_path") or "")
        asset_payload = metadata.get("asset") if isinstance(metadata.get("asset"), dict) else None
        segments_payload = (
            metadata.get("segments") if isinstance(metadata.get("segments"), list) else None
        )
        compile_phase = str(
            metadata.get("compile_phase") or self.hyperrag_v2.config.fast_ingest_compile_phase
        ).strip()
        route_fast_ingest = bool(metadata.get("fast_ingest", False))
        route_pre_compile = bool(metadata.get("pre_compile", False))
        retry_er_chunk_ids = metadata.get("retry_er_chunk_ids")
        if not isinstance(retry_er_chunk_ids, list):
            retry_er_chunk_ids = None

        try:
            if route_fast_ingest and route_pre_compile:
                return TaskResult(
                    status="failed",
                    error="fast_ingest and pre_compile cannot be enabled together",
                )
            if route_fast_ingest and not self.hyperrag_v2.config.fast_ingest_enabled:
                return TaskResult(
                    status="failed",
                    error="fast_ingest route requested but fast_ingest_enabled is false",
                )
            route_workspace = (
                str(
                    getattr(self.hyperrag_v2.config, "precompile_workspace", PRECOMPILED_WORKSPACE)
                    or PRECOMPILED_WORKSPACE
                )
                if route_pre_compile
                else task.workspace
            )
            use_fast_ingest_model = (
                route_fast_ingest
                and self.hyperrag_v2.config.fast_ingest_enabled
                and self.hyperrag_v2.config.fast_ingest_decoding_strategy == "single_model"
                and compile_phase.upper()
                == self.hyperrag_v2.config.fast_ingest_compile_phase.upper()
                and bool(self.hyperrag_v2.config.fast_ingest_model)
            )
            if use_fast_ingest_model:
                experiment = await self.hyperrag_v2._get_kg_experiment(
                    route_workspace,
                    llm_model=self.hyperrag_v2.config.fast_ingest_model,
                    llm_base_url=self.hyperrag_v2.config.fast_ingest_base_url,
                    llm_api_key=self.hyperrag_v2.config.fast_ingest_api_key,
                )
            else:
                experiment = await self.hyperrag_v2._get_kg_experiment(route_workspace)
            route_label = (
                "fast_ingest"
                if route_fast_ingest
                else ("pre_compile" if route_pre_compile else "standard")
            )
            _emit_progress(
                f"KG index task started: task={task.task_id} asset={task.asset_id} "
                f"workspace={route_workspace} "
                f"mode={'preparsed' if asset_payload and segments_payload is not None and asset_path else 'raw'} "
                f"index_route={route_label} "
                f"segments={len(segments_payload or [])} "
                f"llm_model={experiment.config.llm_model} "
                f"fast_ingest_model_active={use_fast_ingest_model} "
                f"ER_extraction_max_async={experiment.config.ER_extraction_max_async}"
            )
            if asset_payload and segments_payload is not None and asset_path:
                result = await experiment.index_preparsed_document(
                    file_path=asset_path,
                    asset_payload=asset_payload,
                    segments_payload=segments_payload,
                    persist_to_kg=not route_fast_ingest,
                    persist_chunk_vectors=True,
                    include_relation_edges=route_fast_ingest,
                    pre_compile_mode=route_pre_compile,
                    retry_er_chunk_ids=retry_er_chunk_ids,
                )
            elif asset_path:
                result = await experiment.index_document(
                    input_path=asset_path,
                    persist_to_kg=not route_fast_ingest,
                    persist_chunk_vectors=True,
                    include_relation_edges=route_fast_ingest,
                    pre_compile_mode=route_pre_compile,
                    retry_er_chunk_ids=retry_er_chunk_ids,
                )
            else:
                return TaskResult(status="failed", error="asset_path not found for KG indexing")

            document_summary = str(result.get("document_summary") or "").strip()
            if document_summary:
                logger.info(
                    "KG index document summary generated: task=%s asset=%s summary=%s",
                    task.task_id,
                    task.asset_id,
                    document_summary,
                )

            timings = result.get("timings") if isinstance(result, dict) else None
            counters = result.get("counters") if isinstance(result, dict) else None
            if isinstance(timings, dict) and isinstance(counters, dict):
                _emit_progress(
                    f"KG index stage summary: task={task.task_id} asset={task.asset_id} "
                    f"chunks={counters.get('chunks')} "
                    f"extracted_entities={counters.get('extracted_entities')} "
                    f"extracted_relations={counters.get('extracted_relations')} "
                    f"entity_nodes={counters.get('entity_nodes')} "
                    f"relation_edges={counters.get('relation_edges')} "
                    f"parsing_chunking_sec={timings.get('parsing_chunking_sec')} "
                    f"entity_relation_extraction_sec={timings.get('entity_relation_extraction_sec')} "
                    f"entity_resolution_sec={timings.get('entity_resolution_sec')} "
                    f"neo4j_upsert_sec={timings.get('neo4j_upsert_sec')} "
                    f"vectorization_sec={timings.get('vectorization_sec')} "
                    f"vector_upsert_sec={timings.get('vector_upsert_sec')} "
                    f"total_sec={timings.get('total_sec')}"
                )

            fast_ingest_stats: Dict[str, Any] | None = None
            if route_fast_ingest:
                relation_edges_raw = (
                    result.get("relation_edges") if isinstance(result, dict) else []
                )
                relation_edges = [
                    item for item in (relation_edges_raw or []) if isinstance(item, dict)
                ]
                if not relation_edges:
                    artifacts = result.get("artifacts") if isinstance(result, dict) else {}
                    if isinstance(artifacts, dict):
                        relation_edges_path = artifacts.get("relation_edges")
                        if relation_edges_path:
                            relation_payload = json.loads(
                                Path(str(relation_edges_path)).read_text(encoding="utf-8")
                            )
                            if isinstance(relation_payload, list):
                                relation_edges = [
                                    item for item in relation_payload if isinstance(item, dict)
                                ]
                if relation_edges:
                    fast_ingest_stats = await self.hyperrag_v2.aingest_fast_relations(
                        workspace=task.workspace,
                        doc_id=task.asset_id,
                        relation_edges=relation_edges,
                        source_file_path=asset_path,
                        compile_phase=compile_phase,
                    )
                    _emit_progress(
                        f"Fast ingest sync: task={task.task_id} asset={task.asset_id} "
                        f"created_edges={fast_ingest_stats.get('created_edges', 0)} "
                        f"absorbed_edges={fast_ingest_stats.get('absorbed_edges', 0)} "
                        f"duplicate_absorb_rate={fast_ingest_stats.get('duplicate_absorb_rate', 0.0)}"
                    )
                else:
                    logger.warning(
                        "Fast ingest route requested but no relation_edges produced: task=%s asset=%s",
                        task.task_id,
                        task.asset_id,
                    )

            failed_er_chunks = result.get("failed_er_chunks") if isinstance(result, dict) else []
            if not isinstance(failed_er_chunks, list):
                failed_er_chunks = []
            error_message: str | None = None
            if failed_er_chunks:
                details = "; ".join(
                    f"{item.get('chunk_id', 'unknown')}: {item.get('error', 'unknown error')}"
                    for item in failed_er_chunks[:20]
                    if isinstance(item, dict)
                )
                suffix = (
                    f"; and {len(failed_er_chunks) - 20} more" if len(failed_er_chunks) > 20 else ""
                )
                error_message = (
                    f"ER extraction failed for {len(failed_er_chunks)} chunk(s); "
                    f"the remaining chunks were indexed. {details}{suffix}"
                )
            if route_pre_compile:
                await get_asset_status_storage().set_status(
                    task.workspace,
                    task.asset_id,
                    AssetStatus.COMPLETED,
                    error_message=error_message or "",
                    metadata={"precompile_failed_er_chunks": failed_er_chunks},
                )

            logger.info(f"KG index task completed: {task.task_id} (asset={task.asset_id})")

            next_stage_segments = _attach_video_chunk_ids(
                segments_payload,
                result.get("segment_chunk_ids") if isinstance(result, dict) else None,
            )
            return TaskResult(
                status="completed",
                data={
                    **result,
                    "document_summary": document_summary,
                    "knowledgebase_summary": "",
                    "fast_ingest": fast_ingest_stats,
                    "pre_compile": route_pre_compile,
                    "asset_path": asset_path,
                    "asset": asset_payload or {},
                    "segments": next_stage_segments,
                },
                error=error_message,
            )
        except Exception as e:
            await get_asset_status_storage().set_status(
                task.workspace, task.asset_id, AssetStatus.FAILED, error_message=str(e)
            )
            logger.exception(f"KG index task failed: {task.task_id}")
            return TaskResult(status="failed", error=str(e))


# ============== BM25 索引 ==============
class BM25IndexHandler(BaseTaskHandler):
    """BM25 全文索引处理器"""

    def __init__(
        self,
        bm25_storage_manager: "BM25StorageManager",
        hyperrag_v2: Any,
    ):
        """
        初始化 BM25 索引处理器

        Args:
            bm25_storage_manager: BM25存储管理器
            hyperrag_v2: HyperRAGV2实例（用于分词）
        """
        self.bm25_storage_manager = bm25_storage_manager
        self.hyperrag_v2 = hyperrag_v2
        self.batch_size = 500  # 批量处理大小

    async def __call__(self, task: "Task") -> TaskResult:
        workspace = task.workspace
        asset_id = task.asset_id

        try:
            chunks = await self.hyperrag_v2.aget_asset_chunks(workspace, asset_id)
            if not chunks:
                logger.warning(f"No chunks found for document {asset_id}")
                return TaskResult(
                    status="completed", data={"chunk_count": 0, "message": "No chunks to index"}
                )

            logger.info(f"BM25 indexing {len(chunks)} chunks for document {asset_id}")

            total_indexed = 0
            for i in range(0, len(chunks), self.batch_size):
                batch_chunks = chunks[i : i + self.batch_size]
                bm25_entries = []
                for chunk_info in batch_chunks:
                    chunk_id = str(chunk_info.get("chunk_id") or "")
                    if not chunk_id:
                        continue
                    content = chunk_info.get("content", "")
                    context = chunk_info.get("context", "")
                    text = f"{content} {context}".strip()
                    if not text:
                        continue
                    tokens = self.hyperrag_v2._jieba_tokenize(text)
                    metadata = (
                        chunk_info.get("metadata")
                        if isinstance(chunk_info.get("metadata"), dict)
                        else {}
                    )

                    bm25_entries.append(
                        {
                            "chunk_id": chunk_id,
                            "text": text,
                            "tokens": tokens,
                            "full_doc_id": chunk_info.get("full_doc_id")
                            or metadata.get("asset_id")
                            or asset_id,
                            "chunk_order_index": chunk_info.get("order", 0),
                        }
                    )

                if bm25_entries:
                    bm25_storage = await self.bm25_storage_manager.get_storage(workspace)
                    await bm25_storage.upsert_batch(workspace, bm25_entries)

                    total_indexed += len(bm25_entries)
                    logger.debug(
                        f"BM25 indexed batch {i // self.batch_size + 1}: {len(bm25_entries)} chunks"
                    )

            logger.info(
                f"BM25 index task completed: {task.task_id} "
                f"(asset={asset_id}, indexed={total_indexed})"
            )

            return TaskResult(
                status="completed",
                data={
                    "asset_id": asset_id,
                    "total_indexed": total_indexed,
                    "chunk_count": len(chunks),
                },
            )

        except Exception as e:
            logger.exception(f"BM25 index task failed: {task.task_id}")
            # BM25 失败不影响主流程，只记录错误
            return TaskResult(status="completed", error=str(e))


# ============== 资源删除 ==============
class DeleteAssetHandler(BaseTaskHandler):
    """删除资源处理器：删除 KG/BM25/视频向量数据并清理本地缓存。"""

    def __init__(
        self,
        bm25_storage_manager: "BM25StorageManager",
        artifact_store: "ArtifactStore",
        hyperrag_v2: Any,
    ):
        """
        初始化删除资源处理器

        Args:
            bm25_storage_manager: BM25储存管理器
            artifact_store: Artifact存储管理器
            hyperrag_v2: HyperRAGV2实例
        """
        self.bm25_storage_manager = bm25_storage_manager
        self.artifact_store = artifact_store
        self.hyperrag_v2 = hyperrag_v2

    async def __call__(self, task: "Task") -> TaskResult:
        workspace = task.workspace
        asset_id = task.asset_id

        try:
            try:
                from ..asset_status import get_asset_status_storage

                st = get_asset_status_storage()
                asset_status = await st.get_status(workspace, asset_id)
                if asset_status:
                    await st.set_status(workspace, asset_id, AssetStatus.DELETING)
            except Exception as e:
                logger.warning(f"Failed to get chunk_ids for deletion of {asset_id}: {e}")

            from ..single_layer_kg import index_storage_path

            asset_status = await get_asset_status_storage().get_status(workspace, asset_id)
            metadata = task.metadata or {}
            file_path = str(
                getattr(asset_status, "file_path", "")
                or metadata.get("asset_path")
                or ""
            )
            fast_ingest_cleanup = await self.hyperrag_v2.adelete_fast_ingest_asset_data(
                workspace=workspace,
                asset_id=asset_id,
            )
            storage_paths: list[str] = []
            for raw_path in (
                file_path,
                str(metadata.get("asset_path") or ""),
                str(metadata.get("previous_file_path") or ""),
            ):
                canonical = index_storage_path(raw_path)
                if canonical and canonical not in storage_paths:
                    storage_paths.append(canonical)
            if storage_paths:
                experiment = await self.hyperrag_v2._get_kg_experiment(workspace)
                deleted: list[Any] = []
                for storage_path in storage_paths:
                    try:
                        deleted.append(await experiment.delete_document(storage_path))
                    except ValueError as exc:
                        if "Document graph not found" not in str(exc):
                            raise
                        deleted.append(
                            {
                                "status": "skipped",
                                "reason": "document_graph_not_found",
                                "file_path": storage_path,
                            }
                        )
                result = deleted[0] if len(deleted) == 1 else {"status": "completed", "paths": deleted}
            else:
                result = {"status": "skipped", "reason": "file_path_not_found"}

            # 3.5 删除 BM25 中的相关数据
            try:
                bm25_storage = await self.bm25_storage_manager.get_storage(
                    workspace, create_if_missing=False
                )
                if bm25_storage:
                    await bm25_storage.delete_by_doc_id(workspace, asset_id)
                    logger.info(f"BM25 data deleted for workspace {workspace}, asset {asset_id}")
            except Exception as e:
                logger.warning(f"Failed to delete BM25 data: {e}")

            # 4. 清理本地缓存
            try:
                # 获取缓存目录
                mineru_cache_dir = self.artifact_store.get_mineru_cache_root(workspace)
                video_cache_dir = self.artifact_store.get_video_cache_root(workspace)

                if task.metadata.get("skip_cache_cleanup"):
                    logger.debug(f"Skipped local cache cleanup for asset {asset_id}")
                else:
                    # 判断是否为视频（从 task.metadata 获取）
                    is_video = task.metadata.get("parse_mode") == "video"

                    cache_dir = (video_cache_dir if is_video else mineru_cache_dir) / str(asset_id)
                    if cache_dir.exists():
                        import shutil

                        shutil.rmtree(cache_dir, ignore_errors=True)
                        logger.debug(f"Cleaned up local cache for asset {asset_id}: {cache_dir}")
            except Exception as e:
                logger.warning(f"Failed to cleanup local cache for {asset_id}: {e}")

            # 返回成功结果
            result_dict = (
                result.to_dict()
                if hasattr(result, "to_dict")
                else (result.__dict__ if hasattr(result, "__dict__") else {"status": str(result)})
            )

            knowledgebase_info = await self.hyperrag_v2.async_knowledgebase_summary(
                workspace,
                asset_id=asset_id,
                remove_asset=True,
            )
            logger.info(
                "Delete asset updated knowledgebase summary: workspace=%s asset=%s remaining_assets=%s",
                workspace,
                asset_id,
                len(knowledgebase_info.asset_ids),
            )

            return TaskResult(
                status="completed",
                data={
                    "deletion_result": result_dict,
                    "fast_ingest_cleanup": fast_ingest_cleanup,
                    "asset_id": asset_id,
                    "workspace": workspace,
                    "knowledgebase_summary": knowledgebase_info.summarization,
                },
            )

        except Exception as e:
            logger.exception(f"Delete asset handler failed for {asset_id}: {e}")
            return TaskResult(status="failed", error=str(e))


class DeleteIndexHandler(BaseTaskHandler):
    """仅删除索引数据，保留缓存目录与 sop-full.json。"""

    def __init__(self, delete_asset_handler: DeleteAssetHandler):
        self.delete_asset_handler = delete_asset_handler

    async def __call__(self, task: "Task") -> TaskResult:
        metadata = dict(task.metadata or {})
        metadata["skip_cache_cleanup"] = True
        delegated_task = task.model_copy(update={"metadata": metadata})
        return await self.delete_asset_handler(delegated_task)


# ============== 术语处理辅助类 ==============
class TerminologyHelper:
    """术语处理辅助类，提供术语相关的工具方法"""

    def __init__(self, hyperrag_v2: Any):
        self.hyperrag_v2 = hyperrag_v2

    @staticmethod
    def normalize_terminology_terms(value: Any) -> List[str]:
        """标准化术语列表"""
        if value is None:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[\n,;，；]", value) if p.strip()]
        elif isinstance(value, list):
            parts = [str(p).strip() for p in value if str(p).strip()]
        else:
            parts = [str(value).strip()] if str(value).strip() else []

        seen = set()
        out: List[str] = []
        for t in parts:
            key = t.casefold() if all(ord(c) < 128 for c in t) else t
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    @staticmethod
    def extract_terminology_text_from_full_doc_content(content: Any) -> str:
        """从术语文档内容中提取文本"""
        if content is None:
            return ""
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            return ""
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                segs = obj.get("segments")
                if isinstance(segs, list) and segs:
                    txt = segs[0].get("text") if isinstance(segs[0], dict) else None
                    if isinstance(txt, str) and txt.strip():
                        return txt.strip()
        except Exception:
            pass
        return content

    @staticmethod
    def parse_json_obj_maybe(value: Any) -> Any:
        """尝试解析 JSON 对象"""
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if not value:
            return None
        try:
            return json.loads(value)
        except Exception:
            return None

    async def resolve_terminology_full_docs(
        self, workspace: str, metadata: Dict[str, Any]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """解析术语文档

        Returns:
            (doc_ids, full_docs_rows)
        """

        if not workspace:
            return [], []

        attributes = metadata.get("attributes") if isinstance(metadata, dict) else None
        if not isinstance(attributes, dict):
            attributes = {}

        terminology_workspace = f"{workspace}_terminology"

        mode = metadata.get("terminology_mode") or (
            attributes.get("terminology_mode") if isinstance(attributes, dict) else None
        )
        mode = str(mode).strip().lower() if mode is not None else ""

        terms = self.normalize_terminology_terms(
            metadata.get("terminology_terms")
            if "terminology_terms" in metadata
            else attributes.get("terminology")
        )
        if not terms:
            return [], []

        threshold_f = metadata.get("terminology_similarity_threshold", 0.5)

        doc_ids: List[str] = []
        if mode == "manual":
            doc_ids = terms
        else:
            matched: Dict[str, float] = {}
            content_by_doc_id: Dict[str, str] = {}

            if threshold_f != threshold_f:  # NaN
                threshold_f = 0.5
            threshold_f = max(-1.0, min(float(threshold_f), 1.0))

            if self.hyperrag_v2 is None:
                return [], []

            try:
                experiment = await self.hyperrag_v2._get_kg_experiment(terminology_workspace)
                embedding_client = experiment._get_embedding_client()
                vector_store = experiment._get_vector_store()
                embeddings = await embedding_client.embed_texts(list(terms))
            except Exception:
                return [], []

            if not isinstance(embeddings, list) or not embeddings:
                return [], []

            for embedding in embeddings:
                try:
                    rows = await vector_store.query_chunks(
                        query_vector=list(embedding),
                        workspace=terminology_workspace,
                        top_k=1,
                        min_similarity=threshold_f,
                    )
                except Exception:
                    continue

                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    normalized = await self.hyperrag_v2._normalize_kg_chunk_row(
                        terminology_workspace,
                        row,
                    )
                    doc_id = str(normalized.get("full_doc_id") or "").strip()
                    if not doc_id:
                        continue
                    try:
                        score_f = float(row.get("similarity") or 0.0)
                    except Exception:
                        score_f = 0.0
                    prev = matched.get(doc_id)
                    if prev is None or score_f > prev:
                        matched[doc_id] = score_f
                        content_by_doc_id[doc_id] = str(normalized.get("content") or "")

            doc_ids = [k for k, _v in sorted(matched.items(), key=lambda kv: kv[1], reverse=True)]
            if not doc_ids:
                return [], []

            return doc_ids, [
                {"id": doc_id, "content": content_by_doc_id.get(doc_id, "")}
                for doc_id in doc_ids
                if content_by_doc_id.get(doc_id)
            ]

        if not doc_ids:
            return [], []

        if self.hyperrag_v2 is None:
            return doc_ids, [{"id": doc_id, "content": doc_id} for doc_id in doc_ids]

        out_rows: List[Dict[str, Any]] = []
        for doc_id in doc_ids:
            try:
                chunks = await self.hyperrag_v2._get_kg_asset_chunks(
                    terminology_workspace,
                    doc_id,
                )
            except Exception:
                chunks = []
            content = "\n".join(
                str(chunk.get("content") or "").strip()
                for chunk in chunks
                if str(chunk.get("content") or "").strip()
            ).strip()
            out_rows.append({"id": doc_id, "content": content or doc_id})

        return doc_ids, out_rows

    def extract_terminology_alias_hotwords_from_full_docs(
        self, rows: List[Dict[str, Any]]
    ) -> List[str]:
        """从术语文档中提取别名热词"""
        keys = (
            "chinese_name",
            "traditional_chinese_name",
            "english_abbreviation",
            "english_full_name",
            "alias",
        )

        hotwords: List[str] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            raw = r.get("content")
            txt = self.extract_terminology_text_from_full_doc_content(raw)
            obj = self.parse_json_obj_maybe(txt)
            if not isinstance(obj, dict):
                obj = self.parse_json_obj_maybe(raw)
            if not isinstance(obj, dict):
                continue

            for k in keys:
                v = obj.get(k)
                if isinstance(v, str):
                    v2 = v.strip()
                    if v2:
                        hotwords.append(v2)

        return self.normalize_terminology_terms(hotwords)

    def format_terminology_prompt_context(
        self, doc_ids: List[str], rows: List[Dict[str, Any]]
    ) -> str:
        """格式化术语提示上下文"""
        if not doc_ids or not rows:
            return ""

        by_id: Dict[str, Any] = {}
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                by_id[r["id"]] = r

        lines: List[str] = []
        for did in doc_ids:
            r = by_id.get(did)
            if not isinstance(r, dict):
                continue
            raw = r.get("content")
            txt = self.extract_terminology_text_from_full_doc_content(raw)
            if not txt:
                continue
            if len(txt) > 800:
                txt = txt[:800] + "…"
            lines.append(f"- {did}: {txt}")

        return "\n".join(lines).strip()

    async def build_terminology_prompt_context(
        self, workspace: str, metadata: Dict[str, Any]
    ) -> str:
        """构建术语提示上下文"""
        doc_ids, rows = await self.resolve_terminology_full_docs(workspace, metadata)
        return self.format_terminology_prompt_context(doc_ids, rows)


# ============== 图片/表格 Caption 增强 ==============
class ImageCaptionHandler(BaseTaskHandler):
    """图片/表格 Caption 增强处理器"""

    def __init__(
        self,
        artifact_store: "ArtifactStore",
        vision_model_func=None,
        max_concurrent_requests: int = 4,
        terminology_helper: Optional["TerminologyHelper"] = None,
    ):
        """
        初始化图片 Caption 处理器

        Args:
            artifact_store: Artifact存储管理器
            vision_model_func: 视觉模型函数
            max_concurrent_requests: 最大并发请求数
            terminology_helper: 术语处理辅助类（可选）
            build_index_content: 构建 KG 索引内容的函数
        """
        self.artifact_store = artifact_store
        self.vision_model_func = vision_model_func
        self.max_concurrent_requests = max_concurrent_requests
        self.terminology_helper = terminology_helper

    async def __call__(self, task: "Task") -> TaskResult:
        from ..image_enhancer import ImageEnhancer

        metadata = task.metadata or {}
        asset_dict = metadata.get("asset")
        segments_list = metadata.get("segments")

        if not asset_dict or not segments_list:
            return TaskResult(status="failed", error="asset/segments missing for image caption")

        try:
            asset = Asset(**asset_dict)
            segments = [Segment(**seg) for seg in segments_list]
        except Exception as e:
            logger.exception(f"ImageCaption: invalid asset/segments: {e}")
            return TaskResult(status="failed", error=f"invalid asset/segments: {e}")

        enhancer = ImageEnhancer(
            self.vision_model_func, max_concurrent_requests=self.max_concurrent_requests
        )
        terminology_prompt_context = ""
        if self.terminology_helper:
            try:
                if isinstance(metadata, dict):
                    terminology_prompt_context = (
                        await self.terminology_helper.build_terminology_prompt_context(
                            task.workspace, metadata
                        )
                    )
            except Exception:
                terminology_prompt_context = ""

        enhanced_segments = await enhancer.enhance(
            asset,
            segments,
            terminology_prompt_context=terminology_prompt_context,
        )

        content = _build_index_content(asset, enhanced_segments)
        enhanced_segments_dump = [_safe_model_dump(seg) for seg in enhanced_segments]

        return TaskResult(
            status="completed",
            data={
                **metadata,
                "content": content,
                "segments": enhanced_segments_dump,
            },
        )


# ============== 视频解析 ==============
class VideoParseHandler(BaseTaskHandler):
    """普通视频解析处理器：调用 actio 的 plain-video 解析能力。"""

    def __init__(
        self,
        artifact_store: "ArtifactStore",
        max_asr_concurrent: int = 8,
        terminology_helper: Optional["TerminologyHelper"] = None,
        actio_config: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化视频解析处理器

        Args:
            artifact_store: Artifact存储管理器
            max_asr_concurrent: 最大 ASR 并发数
            terminology_helper: 术语处理辅助类（可选）
            actio_config: Actio 配置字典（包含 config_dir、models_config_path、device 等）
        """
        self.artifact_store = artifact_store
        self.max_asr_concurrent = max_asr_concurrent
        self.terminology_helper = terminology_helper
        self.actio_config = actio_config or {}

    async def __call__(self, task: "Task") -> TaskResult:
        asset_path = task.metadata.get("asset_path")
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        nested_params = (
            metadata.get("params", {}) if isinstance(metadata.get("params"), dict) else {}
        )

        def _get_param(key: str, default: Any = None) -> Any:
            if key in metadata and metadata.get(key) is not None:
                return metadata.get(key)
            if key in nested_params and nested_params.get(key) is not None:
                return nested_params.get(key)
            return default

        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        title = str(_get_param("title", "") or Path(asset_path).stem)
        edited_sop_full_path = _get_param("edited_sop_full_path")

        # Keep terminology resolution here so callers can pass terminology assets
        # consistently. Actio plain-video parsing owns the actual prompt content.
        t_doc_ids, t_rows = [], []
        if self.terminology_helper:
            try:
                (
                    t_doc_ids,
                    t_rows,
                ) = await self.terminology_helper.resolve_terminology_full_docs(
                    task.workspace, metadata
                )
            except Exception:
                pass

        terminology_prompt_context = ""
        if self.terminology_helper:
            try:
                terminology_prompt_context = (
                    self.terminology_helper.format_terminology_prompt_context(t_doc_ids, t_rows)
                )
            except Exception:
                pass

        video_cache_root = self.artifact_store.get_video_cache_root(task.workspace)
        work_dir = video_cache_root / task.asset_id / "actio_work"
        clip_dir = video_cache_root / task.asset_id / "clips"
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_dir.mkdir(parents=True, exist_ok=True)
        sop_full_path = video_cache_root / task.asset_id / "video-semantic.json"
        sop_full_path.parent.mkdir(parents=True, exist_ok=True)

        required_config_keys = (
            "models_config_path",
            "segment_config",
            "aggregate_config",
            "merge_config",
        )
        missing_config_keys = [
            key for key in required_config_keys if not self.actio_config.get(key)
        ]
        if missing_config_keys:
            return TaskResult(
                status="failed",
                error=(
                    "ViKnow must explicitly provide Actio configuration paths: "
                    + ", ".join(missing_config_keys)
                ),
            )
        models_config_path = Path(self.actio_config["models_config_path"])
        device = self.actio_config.get("device")
        aggregate_config = self.actio_config["aggregate_config"]
        merge_config = self.actio_config["merge_config"]
        segment_config = self.actio_config["segment_config"]

        if ActioVideoParser is None or ActioVideoParserConfig is None:
            return TaskResult(
                status="failed",
                error=(
                    "actio is required for video_parse; install actio or configure the dependency"
                ),
            )

        parser_config = ActioVideoParserConfig(
            segment_config=str(segment_config),
            models_config=str(models_config_path),
            aggregate_config=str(aggregate_config),
            merge_config=str(merge_config),
            device=str(device) if device else None,
            asr_max_concurrency=max(
                1, int(self.actio_config.get("asr_max_concurrency") or self.max_asr_concurrent)
            ),
            annotate_max_concurrency=max(
                1, int(self.actio_config.get("annotate_max_concurrency", 8))
            ),
            intermediate_dir=str(work_dir),
            caption_clip_dir=str(clip_dir),
            keep_asr_segment_outputs=_config_bool(
                self.actio_config.get("keep_asr_segment_outputs"), False
            ),
            keep_asr_audio_outputs=_config_bool(
                self.actio_config.get("keep_asr_audio_outputs"), True
            ),
            keep_intermediate=True,
            segmentation_mode=str(_get_param("segmentation_mode", "general")),
        )
        video_parser = ActioVideoParser(parser_config)

        try:
            parse_kwargs: Dict[str, Any] = {}
            external_transcript = _get_param("external_transcript")
            if external_transcript is not None:
                if not isinstance(external_transcript, dict):
                    raise ValueError("external_transcript must be an object")
                if external_transcript.get("video_id") != task.asset_id:
                    raise ValueError("External transcript belongs to another asset")
                parse_kwargs["external_transcript"] = external_transcript
            course_background = _get_param("course_background")
            if course_background:
                parse_kwargs["course_background"] = course_background
            if edited_sop_full_path:
                parse_kwargs["edited_sop_full_path"] = Path(str(edited_sop_full_path))

            def parse_with_observer():
                from actio.video_caption import observe_caption_generations

                from hyperrag.observability.index_usage import record_generation

                parse_source = _resolve_video_parse_source(
                    asset_path,
                    metadata if isinstance(metadata, dict) else {},
                    nested_params if isinstance(nested_params, dict) else {},
                    workspace=task.workspace,
                    asset_id=task.asset_id,
                )
                with observe_caption_generations(record_generation):
                    return video_parser.parse_video_only(
                        parse_source,
                        video_id=task.asset_id,
                        title=title,
                        output_path=sop_full_path,
                        **parse_kwargs,
                    )

            parse_result = await asyncio.to_thread(
                parse_with_observer,
            )
        except Exception as e:
            logger.exception(f"Actio video parser failed: {e}")
            details = getattr(e, "details", None)
            data = (
                {"video_split": details}
                if isinstance(details, dict)
                and (
                    details.get("timeout_phase") in {"queue", "execution"}
                    or details.get("failure_kind") in {"transport_timeout", "client_deadline"}
                )
                else None
            )
            return TaskResult(status="failed", error=str(e) or type(e).__name__, data=data)

        sop_full_data = parse_result.merged_sop
        if terminology_prompt_context:
            metadata = sop_full_data.setdefault("metadata", {})
            metadata["terminology_context"] = terminology_prompt_context

        semantic_segments = build_video_segments(
            asset_id=task.asset_id,
            sop_full_data=sop_full_data,
            source_parser="video",
        )
        video_source = get_primary_video_source(sop_full_data)
        video_duration = _extract_video_duration(
            sop_full_data,
            getattr(parse_result, "segment_output", None),
        )
        asset_extra = {
            "asset_id": task.asset_id,
            "video_id": video_source["source_id"] or task.asset_id,
            "source_id": video_source["source_id"] or task.asset_id,
            "source_url": video_source["source_url"] or str(asset_path),
            "source_parser": "video",
            "segment_count": len(semantic_segments),
            "actio_models_config_path": str(models_config_path),
        }
        if video_duration is not None:
            asset_extra["duration_sec"] = video_duration
        for key in ("caption_stats", "indexing_outcome", "global_context_quality_warnings"):
            if key in sop_full_data.get("metadata", {}):
                asset_extra[key] = sop_full_data["metadata"][key]
        semantic_asset = {
            "doc_id": task.asset_id,
            "file_path": str(asset_path),
            "doc_type": "video",
            "modality": "video",
            "title": title,
            "extra": asset_extra,
        }
        content = _build_index_content_from_dicts(
            semantic_asset,
            semantic_segments,
        )
        return TaskResult(
            status="completed",
            data={
                "content": content,
                "asset_path": asset_path,
                "asset": semantic_asset,
                "segments": semantic_segments,
                "sop_full": sop_full_data,
            },
        )


class SOPVideoHandler(BaseTaskHandler):
    """
    SOP 视频解析处理器

    使用 actio 的 VideoParser 统一入口执行完整流程：
    - 解析 SOP 文档
    - 视频分割
    - 视频标注
    - 聚合
    - 合并 SOP 和视频 -> sop-full.json
    """

    def __init__(
        self,
        artifact_store: "ArtifactStore",
        llm_config: Optional[Dict[str, Any]] = None,
        embed_config: Optional[Dict[str, Any]] = None,
        actio_config: Optional[Dict[str, Any]] = None,
        max_asr_concurrent: int = 8,
    ):
        """
        初始化 SOP 视频解析处理器

        Args:
            artifact_store: Artifact存储管理器
            llm_config: LLM 配置字典
            embed_config: Embedding 模型配置
            actio_config: Actio 配置字典（包含 config_dir, sop_schema_path 等）
            max_asr_concurrent: 最大 ASR 并发数
        """
        self.artifact_store = artifact_store
        self.llm_config = llm_config or {}
        self.embed_config = embed_config or {}
        self.actio_config = actio_config or {}
        self.max_asr_concurrent = max_asr_concurrent

    async def __call__(self, task: "Task") -> TaskResult:
        """
        执行 SOP 视频解析

        Expected task metadata:
        - asset_path: 视频文件路径
        - sop_doc_path: SOP 文档路径
        """
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        asset_path = metadata.get("asset_path")
        sop_doc_path = metadata.get("sop_doc_path")

        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")
        if not sop_doc_path:
            return TaskResult(status="failed", error="sop_doc_path not found in metadata")

        # 获取 workspace 的缓存目录
        workspace = task.workspace
        video_cache_root = self.artifact_store.get_video_cache_root(workspace)

        # 创建临时工作目录用于 actio 输出
        work_dir = video_cache_root / task.asset_id / "actio_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            required_config_keys = (
                "models_config_path",
                "segment_config",
                "aggregate_config",
                "merge_dtw_config",
                "sop_schema_path",
            )
            missing_config_keys = [
                key for key in required_config_keys if not self.actio_config.get(key)
            ]
            if missing_config_keys:
                raise ValueError(
                    "ViKnow must explicitly provide Actio configuration paths: "
                    + ", ".join(missing_config_keys)
                )
            device = self.actio_config.get("device", "cuda:0")
            models_config_path = Path(self.actio_config["models_config_path"])
            aggregate_config = self.actio_config["aggregate_config"]
            merge_config = self.actio_config["merge_dtw_config"]
            segment_config = self.actio_config["segment_config"]
            sop_schema = self.actio_config["sop_schema_path"]

            if ActioVideoParser is None or ActioVideoParserConfig is None:
                return TaskResult(
                    status="failed",
                    error=(
                        "actio is required for sop_video_parse; "
                        "install actio or configure the dependency"
                    ),
                )

            unified_path = video_cache_root / task.asset_id / "sop-full.json"
            unified_path.parent.mkdir(parents=True, exist_ok=True)

            parser_config = ActioVideoParserConfig(
                segment_config=str(segment_config),
                models_config=str(models_config_path),
                aggregate_config=str(aggregate_config),
                merge_config=str(merge_config),
                sop_schema=str(sop_schema),
                device=str(device) if device else None,
                asr_max_concurrency=max(
                    1,
                    int(self.actio_config.get("asr_max_concurrency") or self.max_asr_concurrent),
                ),
                annotate_max_concurrency=max(
                    1,
                    int(self.actio_config.get("annotate_max_concurrency", 8)),
                ),
                intermediate_dir=str(work_dir),
                keep_asr_segment_outputs=_config_bool(
                    self.actio_config.get("keep_asr_segment_outputs"), False
                ),
                keep_asr_audio_outputs=_config_bool(
                    self.actio_config.get("keep_asr_audio_outputs"), True
                ),
                keep_intermediate=True,
            )
            video_parser = ActioVideoParser(parser_config)

            logger.info(f"SOP video parsing via actio: video={asset_path}, sop={sop_doc_path}")
            parse_result = await asyncio.to_thread(
                video_parser.parse,
                Path(asset_path),
                Path(sop_doc_path),
                output_path=unified_path,
            )
            sop_full_data = parse_result.merged_sop  # 从actio返回的结果中提取有用信息
            if not sop_full_data:
                return TaskResult(status="failed", error="SOP video parsing returned no result")

            if not unified_path.exists():
                unified_path.write_text(
                    json.dumps(sop_full_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            logger.info(f"SOP video parse output written to unified path: {unified_path}")

            video_source = get_primary_video_source(sop_full_data)

            # 从 sop-full.json 中提取 segments
            segments = self._extract_segments_from_sop_full(sop_full_data, task.asset_id)

            # 构建 asset
            video_duration = _extract_video_duration(
                sop_full_data,
                getattr(parse_result, "segment_output", None),
            )
            asset_extra = {
                "asset_id": task.asset_id,
                "video_id": video_source["source_id"] or task.asset_id,
                "source_id": video_source["source_id"] or task.asset_id,
                "source_url": video_source["source_url"],
                "source_parser": "sop_video",
                "sop_title": sop_full_data.get("metadata", {}).get("title", ""),
                "sop_id": sop_full_data.get("metadata", {}).get("sop_id", ""),
            }
            if video_duration is not None:
                asset_extra["duration_sec"] = video_duration
            for key in ("caption_stats", "indexing_outcome", "global_context_quality_warnings"):
                if key in sop_full_data.get("metadata", {}):
                    asset_extra[key] = sop_full_data["metadata"][key]
            asset = {
                "doc_id": task.asset_id,
                "file_path": str(asset_path),
                "doc_type": "sop_video",
                "modality": "video",
                "extra": asset_extra,
            }

            # 构建 KG 索引内容
            content = _build_sop_index_content(sop_full_data)

            return TaskResult(
                status="completed",
                data={
                    "content": content,
                    "asset_path": asset_path,
                    "asset": asset,
                    "segments": segments,
                    "sop_full": sop_full_data,
                },
            )

        except Exception as e:
            logger.exception(f"SOPVideoHandler failed: {e}")
            return TaskResult(status="failed", error=str(e))

    def _extract_segments_from_sop_full(
        self, sop_full_data: Dict[str, Any], asset_id: str
    ) -> List[Dict[str, Any]]:
        """
        从 sop-full.json 中提取视频片段信息

        SOP 的 action_flow 通过 segment_ids 引用顶层 segments。
        """
        return build_video_segments(
            asset_id=asset_id,
            sop_full_data=sop_full_data,
            source_parser="sop_video",
        )


def _build_sop_index_content(sop_data: Dict[str, Any]) -> str:
    """构建 KG 所需的 SOP 内容格式"""
    metadata = sop_data.get("metadata", {})
    overview = sop_data.get("overview", {})
    instructions = sop_data.get("instructions", [])

    lines = []
    lines.append(f"# {metadata.get('title', 'SOP')}")
    lines.append(f"SOP ID: {metadata.get('sop_id', '')}")
    lines.append(f"Version: {metadata.get('version', '')}")
    lines.append("")

    if overview:
        lines.append("## 目的")
        lines.append(overview.get("purpose", ""))
        lines.append("")

        lines.append("## 安全要求")
        lines.append(overview.get("safety", ""))
        lines.append("")

    lines.append("## 操作步骤")
    for step in instructions:
        step_id = step.get("step_id", "")
        content = step.get("content", {})
        title = content.get("title", "")
        description = content.get("description", "")
        safety_notes = content.get("safety_notes", "")

        lines.append(f"### {step_id}: {title}")
        if description:
            lines.append(description)
        if safety_notes:
            lines.append(f"安全提示: {safety_notes}")

        # 添加 action_flow 信息
        action_flow = step.get("action_flow", [])
        for action in action_flow:
            action_desc = action.get("action_description", "")
            if action_desc:
                lines.append(f"- 动作: {action_desc}")

            assertions = action.get("assertions", [])
            for assertion in assertions:
                statement = assertion.get("statement", "")
                category = assertion.get("category", "")
                if statement:
                    lines.append(f"  断言({category}): {statement}")

        lines.append("")

    return "\n".join(lines)


# ============== 工具函数 ==============


def _load_video_transcript(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    transcript_file = (
        (asset.get("extra") or {}).get("transcript_file") if isinstance(asset, dict) else None
    )
    if not transcript_file:
        return []

    path = Path(str(transcript_file))
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception(f"Failed to read transcript file: {path}")
        return []

    return payload if isinstance(payload, list) else []


def _safe_model_dump(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()  # type: ignore[return-value]
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _build_plain_video_semantic_asset(
    *,
    asset_id: str,
    asset: Dict[str, Any],
    asset_path: str,
    segment_count: int,
) -> Dict[str, Any]:
    extra = dict(asset.get("extra") or {})
    extra.update(
        {
            "asset_id": asset_id,
            "video_id": asset_id,
            "source_id": asset_id,
            "source_parser": "video",
        }
    )
    return {
        "doc_id": asset_id,
        "source_uri": asset.get("source_uri") or asset_path,
        "file_path": asset_path,
        "modality": "video",
        "doc_type": "video",
        "title": asset.get("title") or Path(asset_path).stem,
        "language": asset.get("language"),
        "segments_count": segment_count,
        "extra": extra,
        "page_info": asset.get("page_info"),
        "created_at": asset.get("created_at"),
    }


def _build_index_content_from_dicts(asset: Dict[str, Any], segments: List[Dict[str, Any]]) -> str:
    """构建 KG 所需的内容格式（dict 版本）。"""
    index_input = {
        "asset": {
            "doc_id": asset.get("doc_id"),
            "source_uri": asset.get("source_uri"),
            "modality": asset.get("modality"),
            "doc_type": asset.get("doc_type"),
            "title": html.unescape(str(asset.get("title") or "")) or None,
            "language": asset.get("language"),
            "segments_count": asset.get("segments_count"),
            "extra": asset.get("extra") or {},
            "page_info": asset.get("page_info"),
            "created_at": asset.get("created_at"),
        },
        "segments": [
            {
                "segment_id": seg.get("segment_id"),
                "doc_id": seg.get("doc_id"),
                "order": seg.get("order"),
                "type": seg.get("type"),
                "text": html.unescape(str(seg.get("text") or "")),
                "main_struct": seg.get("main_struct") or {},
                "location": seg.get("location"),
                "extra": {
                    k: html.unescape(v) if isinstance(v, str) else v
                    for k, v in (seg.get("extra") or {}).items()
                },
            }
            for seg in segments
        ],
    }
    return json.dumps(index_input, ensure_ascii=False)


def _build_index_content(asset: Any, segments: List[Any]) -> str:
    """构建 KG 所需的内容格式"""
    index_input = {
        "asset": {
            "doc_id": asset.doc_id,
            "source_uri": asset.source_uri,
            "modality": asset.modality,
            "title": html.unescape(asset.title or "") or None,
            "language": asset.language,
            "segments_count": asset.segments_count,
            "extra": asset.extra,
            "page_info": asset.page_info,
            "created_at": asset.created_at,
        },
        "segments": [
            {
                "segment_id": seg.segment_id,
                "doc_id": seg.doc_id,
                "order": seg.order,
                "type": seg.type,
                "text": html.unescape(seg.text or ""),
                "main_struct": seg.main_struct,
                "location": seg.location,
                "extra": {
                    k: html.unescape(v) if isinstance(v, str) else v
                    for k, v in (seg.extra or {}).items()
                },
            }
            for seg in segments
        ],
    }
    return json.dumps(index_input, ensure_ascii=False)
