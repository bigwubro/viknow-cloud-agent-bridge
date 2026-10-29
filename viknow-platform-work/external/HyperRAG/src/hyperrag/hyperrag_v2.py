"""
HyperRAGV2 单一入口（集成任务管理，多 workspace）

约定：
- 异步接口统一以 a 开头（aquery/aget_chunk/...）
- 同步接口为不带 a 的同名方法（query/get_chunk/...）
- 对外返回数据结构与 `hyperrag.py` 保持一致（尤其是 chunk/asset）
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import json
import logging
import math
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import jieba
from langfuse import get_client

from .artifact_store import ArtifactStore
from .asset_status import (
    AssetProcessingStatus,
    AssetStatus,
    get_asset_status_storage,
)
from .bm25_storage_manager import BM25StorageManager
from .config_loader import HyperRAGConfig, TaskLimitConfig, build_default_multimodal_embedder
from .discovery_tokens import query_terms
from .fast_ingest_runtime import FastIngestRelation, FastIngestRuntime
from .kg import (
    PRECOMPILED_LIBRARY_ID,
    PRECOMPILED_WORKSPACE,
    normalize_kg_query_output,
)
from .knowledge_discovery import (
    KnowledgeDiscoveryAsset,
    KnowledgeDiscoveryLibrary,
    asset_to_dict,
    catalog_content,
    get_knowledge_discovery_catalog_storage,
    library_to_dict,
    normalize_search_text,
)
from .knowledgebase_info import KnowledgebaseInfo, get_knowledgebase_info_storage
from .knowledgebase_summary import (
    EMPTY_KNOWLEDGEBASE_SUMMARY,
    build_asset_line,
    compose_knowledgebase_summary,
    fallback_overview_from_assets,
    normalize_text,
    shorten_text,
)
from .runtime_primitives import (
    QueryParam,
    generate_reference_list_from_chunks,
    initialize_share_data,
)
from .single_layer_kg import (
    SINGLE_LAYER_ENTITY_LABEL,
    SINGLE_LAYER_RELATION_TYPE,
    SingleLayerKGService,
    index_storage_path,
)
from .task import DistributedTaskQueue, TaskRegistry, TaskType
from .task.handler import (
    BM25IndexHandler,
    DeleteAssetHandler,
    DeleteIndexHandler,
    DocParserHandler,
    ImageCaptionHandler,
    KGIndexHandler,
    SOPVideoHandler,
    TerminologyHelper,
    VideoParseHandler,
)
from .task.job_builder import JobBuilder
from .task.models import JobInstance, Task, TaskResource, TaskStatus
from .task.scheduler import TaskScheduler
from .utils.job_log import setup_logger
from .utils.segment_tools import build_video_segments, get_primary_video_source

logger = logging.getLogger(__name__)


class VideoUpdateSubmissionUnknown(RuntimeError):
    """The scheduler may have accepted this job; never replay it automatically."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"video update submission uncertain; inspect job {job_id} before retrying")


def _write_video_summary_text(path: Path, text: str, *, source_path: Path | None = None) -> None:
    """Atomic replacement prevents readers seeing half-written editable metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    permission_source = path if path.exists() else source_path
    mode = stat.S_IMODE(permission_source.stat().st_mode) if permission_source is not None else None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".summary-", delete=False
        ) as output:
            temporary = Path(output.name)
            if mode is not None:
                os.fchmod(output.fileno(), mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class HyperRAGV2:
    """
    HyperRAG v2（带任务管理，多 workspace）。

    注意：本类按 workspace 创建并缓存 SingleLayerKGService。
    """

    def __init__(
        self,
        config: HyperRAGConfig | None = None,
        config_path: str = "hyperrag.yml",
        on_job_callback: Callable | None = None,
        on_task_callback: Callable | None = None,
        multimodal_embedder: Any | None = None,
    ):
        if config is None:
            raise ValueError("HyperRAGV2 requires a HyperRAGConfig instance")
        self.config = config

        self.instance_id = self.config.instance_id
        self._user_on_job_callback = on_job_callback
        self._user_on_task_callback = on_task_callback
        self.multimodal_embedder = multimodal_embedder
        if self.multimodal_embedder is None:
            self.multimodal_embedder = build_default_multimodal_embedder(self.config)

        self._queue: DistributedTaskQueue | None = None
        self.artifact_store: ArtifactStore | None = None
        self.scheduler: TaskScheduler | None = None
        self.terminology_helper: TerminologyHelper | None = TerminologyHelper(hyperrag_v2=self)
        self.bm25_storage_manager: BM25StorageManager | None = None
        self.fast_ingest_runtime: FastIngestRuntime | None = None
        self._post_compile_task: asyncio.Task | None = None
        self._post_compile_wakeup = asyncio.Event()

        self.bm25_stopwords: set[str] = set()

        # workspace -> status storage cache
        self._status_storages: dict[str, Any] = {}
        self._status_storages_lock = asyncio.Lock()

        # workspace -> asset status cache for query-time chunk hydration
        self._workspace_asset_status_cache: dict[str, dict[str, AssetProcessingStatus]] = {}
        self._workspace_asset_status_cache_lock = asyncio.Lock()

        # (workspace, llm_model, llm_base_url) -> single-layer KG experiment
        self._kg_experiments: dict[tuple[str, str, str], SingleLayerKGService] = {}
        self._kg_experiments_lock = asyncio.Lock()

        # (workspace, file_id) -> parse_mode
        self._parse_mode_cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._parse_mode_cache_lock = asyncio.Lock()
        self._parse_mode_cache_ttl_sec = 30.0
        self._parse_mode_cache_max = 2048

        # (workspace, parse_mode, file_id) -> snapshot (asset/segments/transcript + seg_map)
        self._snapshot_cache: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._snapshot_cache_lock = asyncio.Lock()
        self._snapshot_cache_ttl_sec = 60.0
        self._snapshot_cache_max = 512

        self._runtime_init_lock = asyncio.Lock()
        self._core_initialized = False
        self._knowledge_discovery_catalog_storage = None
        self._knowledge_discovery_catalog_lock = asyncio.Lock()
        self._knowledge_discovery_catalog_query_semaphore: asyncio.Semaphore | None = None
        self._knowledge_discovery_catalog_query_semaphore_limit: int | None = None
        self._knowledge_discovery_backfill_ready = asyncio.Event()
        self._knowledge_discovery_backfill_task: asyncio.Task[None] | None = None
        self._bm25_runtime_initialized = False
        self._task_runtime_initialized = False
        self._handlers_registered = False

        self._initialized = False

        logger.info(f"HyperRAGV2 instance created: {self.instance_id}")

    async def _get_parse_mode_cached(self, workspace: str, file_id: str) -> str:
        """
        Cache parse_mode lookups to avoid repeated AssetStatusStorage reads when
        multiple chunks from the same file are requested in a short time window.

        TODO 这个Cache的实现有点丑
        """
        key = (str(workspace), str(file_id))
        now = time.monotonic()

        async with self._parse_mode_cache_lock:
            hit = self._parse_mode_cache.get(key)
            if hit and (now - float(hit.get("ts", 0.0))) <= self._parse_mode_cache_ttl_sec:
                # LRU refresh
                self._parse_mode_cache.move_to_end(key, last=True)
                return str(hit.get("parse_mode") or "document")

        st = get_asset_status_storage()
        status = await st.get_status(workspace, file_id)
        parse_mode = status.parse_mode if status and hasattr(status, "parse_mode") else "document"
        if not parse_mode:
            parse_mode = "document"

        async with self._parse_mode_cache_lock:
            self._parse_mode_cache[key] = {"ts": now, "parse_mode": parse_mode}
            self._parse_mode_cache.move_to_end(key, last=True)
            while len(self._parse_mode_cache) > self._parse_mode_cache_max:
                self._parse_mode_cache.popitem(last=False)

        return str(parse_mode)

    async def _get_file_snapshot_cached(
        self,
        workspace: str,
        parse_mode: str,
        file_id: str,
        mineru_cache_dir: Path,
        video_cache_dir: Path,
    ) -> dict[str, Any]:
        """
        Cache mineru/video cache snapshots to avoid repeated disk reads/parses when
        multiple chunks from the same file are requested.

        Returns a dict with keys: asset, segments, transcript, seg_map

        TODO 这个Cache的实现有点丑
        """
        key = (str(workspace), str(parse_mode), str(file_id))
        now = time.monotonic()

        async with self._snapshot_cache_lock:
            hit = self._snapshot_cache.get(key)
            if hit and (now - float(hit.get("ts", 0.0))) <= self._snapshot_cache_ttl_sec:
                self._snapshot_cache.move_to_end(key, last=True)
                return hit

        asset: dict[str, Any] | None = None
        segments: list[dict[str, Any]] = []
        transcript: list[dict[str, Any]] = []

        if parse_mode == "sop_video":
            asset, segments, transcript = await self._load_sop_video_snapshot(
                video_cache_dir, file_id, source_parser="sop_video"
            )
            # Backward compatibility: old SOP runs may only have index.json clips.
            if not segments:
                asset, segments, transcript = await self._load_video_cache_snapshot(
                    video_cache_dir, file_id
                )
        elif parse_mode == "video":
            asset, segments, transcript = await self._load_sop_video_snapshot(
                video_cache_dir, file_id, source_parser="video"
            )
        else:
            asset, segments = await self._load_mineru_cache_snapshot(mineru_cache_dir, file_id)

        seg_map: dict[str, dict[str, Any]] = {}
        for s in segments or []:
            sid = s.get("segment_id")
            if sid is not None:
                seg_map[str(sid)] = s

        entry = {
            "ts": now,
            "asset": asset,
            "segments": segments,
            "transcript": transcript,
            "seg_map": seg_map,
        }

        async with self._snapshot_cache_lock:
            self._snapshot_cache[key] = entry
            self._snapshot_cache.move_to_end(key, last=True)
            while len(self._snapshot_cache) > self._snapshot_cache_max:
                self._snapshot_cache.popitem(last=False)

        return entry

    async def aget_file_snapshot(self, workspace: str, file_id: str) -> dict[str, Any]:
        """
        Public API: return a cached snapshot for a file.

        This avoids repeated disk reads/parses of mineru/video cache files when
        multiple chunks from the same file are requested.

        Returns a dict with keys: parse_mode, asset, segments, transcript, seg_map
        """
        ws = str(workspace)
        fid = str(file_id)
        parse_mode = await self._get_parse_mode_cached(ws, fid)

        # NOTE: this API must stay lightweight. Do NOT force-initialize the whole
        # task system (Redis/scheduler) just to read local cache snapshots.
        mineru_cache_dir, video_cache_dir = self._get_workspace_cache_dirs(ws)

        snap = await self._get_file_snapshot_cached(
            workspace=ws,
            parse_mode=parse_mode,
            file_id=fid,
            mineru_cache_dir=mineru_cache_dir,
            video_cache_dir=video_cache_dir,
        )
        # Keep return payload stable and minimal
        return {
            "parse_mode": parse_mode,
            "asset": snap.get("asset"),
            "segments": snap.get("segments") or [],
            "transcript": snap.get("transcript") or [],
            "seg_map": snap.get("seg_map") or {},
        }

    async def initialize(self):
        """异步初始化完整任务运行时（一次）。"""
        if self._task_runtime_initialized:
            return

        await self._ensure_task_runtime()

    async def initialize_readonly(self) -> None:
        """Initialize storage access without starting task queues or schedulers."""
        await self._ensure_core_runtime()

    async def _ensure_core_runtime(self) -> None:
        if self._core_initialized:
            return

        async with self._runtime_init_lock:
            if self._core_initialized:
                return

            logger.info("Initializing HyperRAGV2 core runtime...")

            self._configure_storage_env()
            initialize_share_data(workers=1)
            logger.info("Shared storage data initialized")

            await get_knowledgebase_info_storage().initialize()
            discovery_storage = get_knowledge_discovery_catalog_storage()
            await discovery_storage.initialize()

            if self.artifact_store is None:
                self.artifact_store = ArtifactStore(self.config)

            self._knowledge_discovery_catalog_storage = discovery_storage
            self._core_initialized = True
            self._knowledge_discovery_backfill_task = asyncio.create_task(
                self._backfill_knowledge_discovery_catalog(),
                name="hyperrag-knowledge-discovery-backfill",
            )

    async def _backfill_knowledge_discovery_catalog(self) -> None:
        """Reconcile materialized content; identical snapshots require no write transaction."""
        try:
            info_storage = get_knowledgebase_info_storage()
            records = await info_storage.get_all()
            storage = self._knowledge_discovery_catalog_storage
            libraries = {item.library_id: item for item in await storage.list_libraries()}
            assets_by_library: dict[str, list[KnowledgeDiscoveryAsset]] = {}
            for asset in await storage.list_all_assets():
                assets_by_library.setdefault(asset.library_id, []).append(asset)
            for record in records:
                library, assets = self._discovery_catalog_snapshot(record)
                current = libraries.get(record.id)
                expected = {item.asset_id: catalog_content(item) for item in assets}
                actual = {
                    item.asset_id: catalog_content(item)
                    for item in assets_by_library.get(record.id, [])
                    if item.is_active or item.asset_id in expected
                }
                if (
                    current is None
                    or catalog_content(current) != catalog_content(library)
                    or actual != expected
                ):
                    await self._synchronize_knowledge_discovery_catalog(record)
            source_ids = {record.id for record in records}
            for orphan_id in libraries.keys() - source_ids:
                async with storage.library_transaction(orphan_id) as transaction:
                    latest = await info_storage.with_db(transaction.db).get(orphan_id)
                    if latest is None:
                        await transaction.delete_library(orphan_id)
            logger.info(
                "Knowledge discovery catalog backfill completed for %s libraries",
                len(records),
            )
        except asyncio.CancelledError as exc:
            self._knowledge_discovery_backfill_error = exc
            raise
        except Exception as exc:
            self._knowledge_discovery_backfill_error = exc
            logger.exception("Knowledge discovery catalog backfill failed")
        finally:
            self._knowledge_discovery_backfill_ready.set()

    async def _wait_for_knowledge_discovery_catalog(self) -> None:
        ready = getattr(self, "_knowledge_discovery_backfill_ready", None)
        if ready is not None:
            await ready.wait()
        error = getattr(self, "_knowledge_discovery_backfill_error", None)
        if error is not None:
            raise RuntimeError("Knowledge discovery catalog backfill failed") from error

    def _configure_storage_env(self) -> None:
        addon_params = self.config.addon_params or {}
        env_mappings = {
            "POSTGRES_USER": addon_params.get("postgres_user"),
            "POSTGRES_PASSWORD": addon_params.get("postgres_password"),
            "POSTGRES_HOST": addon_params.get("postgres_host"),
            "POSTGRES_PORT": addon_params.get("postgres_port"),
            "POSTGRES_DATABASE": addon_params.get("postgres_database"),
            "POSTGRES_MAX_CONNECTIONS": addon_params.get("postgres_max_connections"),
        }
        for env_name, env_value in env_mappings.items():
            if env_value is not None and env_value != "":
                os.environ[env_name] = str(env_value)

    async def _ensure_bm25_runtime(self) -> None:
        await self._ensure_core_runtime()
        if self._bm25_runtime_initialized:
            return

        async with self._runtime_init_lock:
            if self._bm25_runtime_initialized:
                return

            if self.bm25_storage_manager is None:
                self.bm25_storage_manager = BM25StorageManager(self.config)
                logger.info("BM25 storage manager initialized")

            if self.config.bm25_enabled:
                await self._load_jieba_custom_dict()
                await self._load_bm25_stopwords()
            else:
                self.bm25_stopwords = self._get_default_stopwords()

            self._bm25_runtime_initialized = True

    async def _ensure_task_runtime(self) -> None:
        if self._task_runtime_initialized:
            return

        await self._ensure_bm25_runtime()

        async with self._runtime_init_lock:
            if self._task_runtime_initialized:
                return

            logger.info("Initializing HyperRAGV2 task runtime...")

            # JOB_LOG_DIR is injected through runtime config, so initialize the
            # per-job sink here after the environment is fully available.
            setup_logger()

            self._queue = DistributedTaskQueue(self.config.redis_uri)
            await self._queue.initialize()
            if self.config.fast_ingest_enabled:
                self.fast_ingest_runtime = FastIngestRuntime(self._queue.redis)

            if not self._handlers_registered:
                self._register_all_handlers()
                self._handlers_registered = True

            task_limits = self._compute_task_limits()
            enable_rw_lock = self.config.enable_rw_lock

            self.scheduler = TaskScheduler(
                queue=self._queue,
                instance_id=self.instance_id,
                task_limits=task_limits,
                max_concurrent_job_tasks=self.config.max_concurrent_job_tasks,
                enable_rw_lock=enable_rw_lock,
                on_job_callback=self._on_job_callback,
                on_task_callback=self._on_task_callback,
                unified_scheduler=self._build_unified_scheduler(),
            )

            if self.config.initial_mode == "read_write":
                await self.scheduler.switch_to_rw()

            await self.scheduler.start_heartbeat(interval=self.config.heartbeat_interval)
            if (
                self.config.fast_ingest_enabled
                and self.config.post_compile_auto_enabled
                and self.config.initial_mode == "read_write"
            ):
                self._post_compile_task = asyncio.create_task(self._post_compile_loop())

            self._task_runtime_initialized = True
            self._initialized = True
            logger.info("HyperRAGV2 initialized successfully")

    def _build_unified_scheduler(self):
        """根据配置构建 scheduler-core 统一调度器实例。

        仅在 ``config.scheduler_unified.enabled`` 为真时返回实例，否则返回 None
        （TaskScheduler 保持原 HyperRAG 调度逻辑）。返回的实例与 ViKnow 侧
        UnifiedSchedulerBridge 各自独立构造，但共享同一 Redis 数据空间
        （scheduler:* namespace），因此队列状态与租约一致。
        """
        if not self.config.scheduler_unified or not self.config.scheduler_unified.enabled:
            return None
        try:
            from scheduler_core.config import (
                DomainConfig,
                SchedulerConfig,
                default_domains,
            )
            from scheduler_core.scheduler import UnifiedScheduler
            from scheduler_core.store import SchedulerStore

            uc = self.config.scheduler_unified
            domain_configs = (
                {name: DomainConfig(**cfg) for name, cfg in uc.domains.items()}
                if uc.domains
                else default_domains()
            )
            # 保证 reference_domain（vllm）始终存在，避免 SchedulerConfig 校验失败
            if "vllm" not in domain_configs:
                domain_configs["vllm"] = default_domains()["vllm"]
            scheduler_config = SchedulerConfig(
                enabled=True,
                redis_url=uc.redis_url,
                domains=domain_configs,
                lease_ttl_seconds=uc.lease_ttl_seconds,
                heartbeat_interval_seconds=uc.heartbeat_interval_seconds,
                queue_wait_timeout_seconds=uc.queue_wait_timeout_seconds,
                default_execution_timeout_seconds=uc.default_execution_timeout_seconds,
            )
            store = SchedulerStore.from_url(scheduler_config.redis_url, scheduler_config)
            return UnifiedScheduler(scheduler_config, store)
        except Exception as e:  # noqa: BLE001 - 配置错误应快速失败并显式暴露
            logger.error(f"Failed to build unified scheduler: {e}")
            raise

    async def _ensure_fast_ingest_runtime(self) -> FastIngestRuntime | None:
        await self._ensure_task_runtime()
        if not self.config.fast_ingest_enabled:
            return None
        if self.fast_ingest_runtime is None:
            assert self._queue is not None
            self.fast_ingest_runtime = FastIngestRuntime(self._queue.redis)
        return self.fast_ingest_runtime

    async def aingest_fast_relations(
        self,
        *,
        workspace: str,
        doc_id: str,
        relation_edges: list[dict[str, Any]],
        source_file_path: str | None = None,
        compile_phase: str | None = None,
    ) -> dict[str, Any]:
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            return {
                "workspace": workspace,
                "doc_id": doc_id,
                "skipped": True,
                "reason": "fast_ingest_disabled",
            }

        normalized_edges: list[FastIngestRelation] = []
        for edge in relation_edges:
            source_entity = str(edge.get("source_entity") or "").strip()
            target_entity = str(edge.get("target_entity") or "").strip()
            relation_description = str(edge.get("relationship_description") or "").strip()
            relation_type = str(edge.get("relation_type") or relation_description or "").strip()
            chunk_ids = edge.get("chunk_ids") if isinstance(edge.get("chunk_ids"), list) else []
            chunk_id = str(chunk_ids[0] if chunk_ids else "")
            edge_file_path = str(edge.get("file_path") or source_file_path or "").strip()
            if not source_entity or not target_entity:
                continue
            normalized_edges.append(
                FastIngestRelation(
                    source_entity=source_entity,
                    target_entity=target_entity,
                    relation_type=relation_type,
                    relation_description=relation_description,
                    chunk_id=chunk_id,
                    file_path=edge_file_path,
                )
            )
        phase = str(compile_phase or self.config.fast_ingest_compile_phase or "FAST_INGEST")
        result = await runtime.ingest_relations(
            workspace=workspace,
            doc_id=doc_id,
            relations=normalized_edges,
            compile_phase=phase,
        )
        if result.get("total_relations"):
            self._post_compile_wakeup.set()
        return result

    async def _post_compile_loop(self) -> None:
        interval_seconds = max(1, int(self.config.post_compile_auto_interval_minutes or 15) * 60)
        while True:
            interval_elapsed = False
            try:
                await asyncio.wait_for(self._post_compile_wakeup.wait(), timeout=interval_seconds)
            except TimeoutError:
                interval_elapsed = True
            except asyncio.CancelledError:
                return
            finally:
                self._post_compile_wakeup.clear()

            try:
                runtime = await self._ensure_fast_ingest_runtime()
                if runtime is None:
                    continue
                for workspace in await runtime.list_workspaces():
                    await self.aauto_post_compile_batch(
                        workspace=workspace,
                        interval_elapsed=interval_elapsed,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Automatic fast ingest post-compile cycle failed")

    async def aauto_post_compile_batch(
        self,
        *,
        workspace: str,
        interval_elapsed: bool,
    ) -> dict[str, Any]:
        if not self.config.post_compile_auto_enabled:
            return {"workspace": workspace, "skipped": True, "reason": "post_compile_auto_disabled"}
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            return {"workspace": workspace, "skipped": True, "reason": "fast_ingest_disabled"}
        edge_count = await runtime.unverified_edge_count(workspace=workspace)
        threshold = max(1, int(self.config.post_compile_hot_buffer_edge_threshold or 1))
        if not interval_elapsed and edge_count < threshold:
            return {
                "workspace": workspace,
                "skipped": True,
                "reason": "below_hot_buffer_threshold",
                "unverified_edges": edge_count,
                "threshold": threshold,
            }
        return await self.apost_compile_batch(workspace=workspace)

    async def _query_vkr_delta_graph(
        self,
        *,
        workspace: str,
        query: str,
        top_k: int,
        entity_top_k: int,
    ) -> dict[str, Any]:
        if not self.config.vkr_enabled:
            return {"chunks": [], "entities": [], "relationships": [], "stats": {}}
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            return {"chunks": [], "entities": [], "relationships": [], "stats": {}}
        return await runtime.query_delta_graph(
            workspace=workspace,
            query=query,
            top_k_relations=top_k,
            top_k_entities=entity_top_k,
        )

    async def _query_vkr_delta_chunks(
        self,
        *,
        workspace: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        delta_graph = await self._query_vkr_delta_graph(
            workspace=workspace,
            query=query,
            top_k=top_k,
            entity_top_k=max(1, int(self.config.kg_vector_entity_top_k or 10)),
        )
        return list(delta_graph.get("chunks") or [])

    @staticmethod
    def _split_graph_source_id(source_id: Any) -> list[str]:
        value = str(source_id or "").strip()
        if not value:
            return []
        return [item for item in value.split("<SEP>") if item]

    @staticmethod
    def _merge_graph_source_ids(*source_ids: Any) -> str:
        normalized: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids:
            for chunk_id in HyperRAGV2._split_graph_source_id(source_id):
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                normalized.append(chunk_id)
        return "<SEP>".join(normalized)

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _entity_score(entity: dict[str, Any]) -> float:
        return max(
            HyperRAGV2._safe_float(entity.get("weight")),
            HyperRAGV2._safe_float(entity.get("score")),
            HyperRAGV2._safe_float(entity.get("vector_score")),
        )

    @staticmethod
    def _relation_score(relation: dict[str, Any]) -> float:
        return max(
            HyperRAGV2._safe_float(relation.get("weight")),
            HyperRAGV2._safe_float(relation.get("score")),
            HyperRAGV2._safe_float(relation.get("vector_score")),
        )

    @staticmethod
    def _merge_entities(
        base_entities: list[dict[str, Any]],
        delta_entities: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> tuple[list[dict[str, Any]], int]:
        merged: dict[str, dict[str, Any]] = {}
        for entity in [*base_entities, *delta_entities]:
            name = str(entity.get("entity_name") or "").strip()
            if not name:
                continue
            key = name.lower()
            score = HyperRAGV2._entity_score(entity)
            source_id = str(entity.get("source_id") or "")
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "entity_name": name,
                    "entity_type": str(entity.get("entity_type") or "Concept"),
                    "description": str(entity.get("description") or ""),
                    "source_id": source_id,
                    "file_path": str(entity.get("file_path") or ""),
                    "weight": score,
                }
                continue
            if score > HyperRAGV2._safe_float(current.get("weight")):
                current["weight"] = score
                current["entity_type"] = str(
                    entity.get("entity_type") or current.get("entity_type") or "Concept"
                )
                current["description"] = str(
                    entity.get("description") or current.get("description") or ""
                )
                current["file_path"] = str(
                    entity.get("file_path") or current.get("file_path") or ""
                )
            current["source_id"] = HyperRAGV2._merge_graph_source_ids(
                current.get("source_id"), source_id
            )

        ordered = sorted(
            merged.values(),
            key=lambda item: HyperRAGV2._safe_float(item.get("weight")),
            reverse=True,
        )
        pruned = max(0, len(ordered) - top_k)
        return ordered[:top_k], pruned

    @staticmethod
    def _merge_relationships(
        base_relationships: list[dict[str, Any]],
        delta_relationships: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> tuple[list[dict[str, Any]], int]:
        merged: dict[str, dict[str, Any]] = {}
        for relation in [*base_relationships, *delta_relationships]:
            src_id = str(relation.get("src_id") or "").strip()
            tgt_id = str(relation.get("tgt_id") or "").strip()
            description = str(relation.get("description") or "").strip()
            if not src_id or not tgt_id:
                continue
            key = f"{src_id.lower()}|{tgt_id.lower()}|{description.lower()}"
            score = HyperRAGV2._relation_score(relation)
            source_id = str(relation.get("source_id") or "")
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "src_id": src_id,
                    "tgt_id": tgt_id,
                    "description": description,
                    "weight": score,
                    "source_id": source_id,
                    "file_path": str(relation.get("file_path") or ""),
                    "keywords": relation.get("keywords") or [],
                    "situation": str(relation.get("situation") or ""),
                }
                continue
            if score > HyperRAGV2._safe_float(current.get("weight")):
                current["weight"] = score
                current["description"] = description or str(current.get("description") or "")
                current["file_path"] = str(
                    relation.get("file_path") or current.get("file_path") or ""
                )
                if relation.get("keywords"):
                    current["keywords"] = relation.get("keywords")
                current["situation"] = str(
                    relation.get("situation") or current.get("situation") or ""
                )
            current["source_id"] = HyperRAGV2._merge_graph_source_ids(
                current.get("source_id"), source_id
            )

        ordered = sorted(
            merged.values(),
            key=lambda item: HyperRAGV2._safe_float(item.get("weight")),
            reverse=True,
        )
        pruned = max(0, len(ordered) - top_k)
        return ordered[:top_k], pruned

    @staticmethod
    def _collect_source_chunk_ids(
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> list[str]:
        chunk_ids: list[str] = []
        seen: set[str] = set()
        for item in [*entities, *relationships]:
            for chunk_id in HyperRAGV2._split_graph_source_id(item.get("source_id")):
                if not chunk_id or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                chunk_ids.append(chunk_id)
        return chunk_ids

    @staticmethod
    def _union_vkr_chunks(
        base_chunks: list[dict[str, Any]],
        delta_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not delta_chunks:
            return list(base_chunks)
        merged = list(base_chunks)
        seen: set[str] = set()
        for chunk in base_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id:
                seen.add(chunk_id)
        for chunk in delta_chunks:
            key = str(chunk.get("chunk_id") or "")
            if not key:
                key = str((chunk.get("metadata") or {}).get("vkr_edge_id") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(chunk)
        return merged

    @staticmethod
    def _dedupe_chunks_by_canonical_id(
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep the first candidate for each canonical chunk identity."""
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in chunks:
            metadata = chunk.get("metadata")
            metadata_chunk_id = metadata.get("chunk_id") if isinstance(metadata, dict) else None
            canonical_id = str(metadata_chunk_id or chunk.get("chunk_id") or "").strip()
            if canonical_id and canonical_id in seen:
                continue
            if canonical_id:
                seen.add(canonical_id)
            deduped.append(chunk)
        return deduped

    async def apost_compile_batch(
        self,
        *,
        workspace: str,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            return {"workspace": workspace, "skipped": True, "reason": "fast_ingest_disabled"}
        prepared = await runtime.prepare_post_compile_batch(
            workspace=workspace,
            batch_size=int(batch_size or self.config.post_compile_batch_size),
        )
        batch_id = str(prepared.get("batch_id") or "")
        if not batch_id:
            return {
                "workspace": workspace,
                "batch_id": "",
                "selected_edges": 0,
                "kg_merged_edges": 0,
                "hot_buffer_removed_edges": 0,
                "message": "no_unverified_edges_in_hot_buffer",
            }
        try:
            experiment = await self._get_kg_experiment(workspace)
            edges = [item for item in (prepared.get("edges") or []) if isinstance(item, dict)]
            merged = await experiment.repository.merge_fast_ingest_post_compile_edges(
                workspace=workspace,
                batch_id=batch_id,
                edges=edges,
                compile_phase=str(self.config.fast_ingest_compile_phase or "FAST_INGEST"),
            )
            merged_edges = int(merged.get("merged_edges") or 0)
            if merged_edges != len(edges):
                raise RuntimeError(
                    "post compile graph merge did not persist every selected edge: "
                    f"selected={len(edges)}, merged={merged_edges}"
                )
            vector_storage = await experiment.upsert_fast_ingest_post_compile_vectors(edges)
            finalized = await runtime.finish_post_compile_batch(
                workspace=workspace,
                batch_id=batch_id,
                success=True,
            )
        except asyncio.CancelledError:
            await runtime.finish_post_compile_batch(
                workspace=workspace,
                batch_id=batch_id,
                success=False,
            )
            raise
        except Exception:
            await runtime.finish_post_compile_batch(
                workspace=workspace,
                batch_id=batch_id,
                success=False,
            )
            raise
        return {
            "workspace": workspace,
            "batch_id": batch_id,
            "selected_edges": len(prepared.get("edges") or []),
            "kg_merged_edges": int(merged.get("merged_edges") or 0),
            "kg_merged_entities": int(merged.get("merged_entities") or 0),
            "vector_storage": vector_storage,
            "hot_buffer_removed_edges": int(finalized.get("removed_edges") or 0),
            "message": "post_compile_merged_to_kg_and_cleared_hot_buffer",
        }

    async def alint_batch(
        self,
        *,
        workspace: str,
        max_edges: int | None = None,
    ) -> dict[str, Any]:
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            return {"workspace": workspace, "skipped": True, "reason": "fast_ingest_disabled"}
        experiment = await self._get_kg_experiment(workspace)
        return await experiment.lint_fast_ingest_post_compile_batch(
            workspace=workspace,
            batch_size=int(max_edges or self.config.lint_batch_size),
        )

    def _lint_auto_window_hours(self) -> tuple[int, int]:
        start_hour = int(getattr(self.config, "lint_auto_start_hour", 0) or 0)
        end_hour = int(getattr(self.config, "lint_auto_end_hour", 24) or 24)
        start_hour = min(23, max(0, start_hour))
        end_hour = min(24, max(0, end_hour))
        return start_hour, end_hour

    def _is_in_lint_auto_window(self, now: datetime.datetime | None = None) -> bool:
        current = now or datetime.datetime.now()
        hour = int(current.hour)
        start_hour, end_hour = self._lint_auto_window_hours()
        if start_hour == end_hour:
            return True
        if start_hour < end_hour:
            return start_hour <= hour < end_hour
        return hour >= start_hour or hour < end_hour

    async def aauto_lint_batch(
        self,
        *,
        workspace: str,
        max_edges: int | None = None,
    ) -> dict[str, Any]:
        start_hour, end_hour = self._lint_auto_window_hours()
        if not self._is_in_lint_auto_window():
            return {
                "workspace": workspace,
                "skipped": True,
                "reason": "outside_lint_auto_window",
                "lint_auto_start_hour": start_hour,
                "lint_auto_end_hour": end_hour,
            }
        return await self.alint_batch(workspace=workspace, max_edges=max_edges)

    async def ashow_post_compile_summary(self, *, workspace: str) -> dict[str, Any]:
        experiment = await self._get_kg_experiment(workspace)
        return await experiment.show_fast_ingest_post_compile_summary(workspace=workspace)

    async def aclear_post_compile_injected(self, *, workspace: str) -> dict[str, Any]:
        experiment = await self._get_kg_experiment(workspace)
        return await experiment.clear_fast_ingest_post_compile_injected(workspace=workspace)

    async def alist_lint_issues(
        self,
        *,
        workspace: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        experiment = await self._get_kg_experiment(workspace)
        return await experiment.list_lint_issues(
            workspace=workspace,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def adelete_lint_issue(
        self,
        *,
        workspace: str,
        issue_id: str,
    ) -> dict[str, Any]:
        experiment = await self._get_kg_experiment(workspace)
        return await experiment.delete_lint_issue(
            workspace=workspace,
            issue_id=issue_id,
        )

    async def adelete_fast_ingest_asset_data(
        self,
        *,
        workspace: str,
        asset_id: str,
    ) -> dict[str, Any]:
        runtime = await self._ensure_fast_ingest_runtime()
        if runtime is None:
            hot_buffer = {
                "workspace": workspace,
                "doc_id": asset_id,
                "skipped": True,
                "reason": "fast_ingest_disabled",
            }
        else:
            hot_buffer = await runtime.delete_doc_relations(
                workspace=workspace,
                doc_id=asset_id,
            )
        experiment = await self._get_kg_experiment(workspace)
        post_compile_kg = await experiment.delete_fast_ingest_post_compile_asset(
            workspace=workspace,
            asset_id=asset_id,
        )
        return {
            "workspace": workspace,
            "asset_id": asset_id,
            "hot_buffer": hot_buffer,
            "post_compile_kg": post_compile_kg,
        }

    def _register_all_handlers(self):
        """统一注册所有 handler 实例"""
        # 注册 doc_parse handler
        handler = DocParserHandler(
            artifact_store=self.artifact_store,
            asr_model_func={
                "base_url": self.config.whisper_base_url,
                "api_key": self.config.whisper_api_key,
            },
        )
        TaskRegistry.register(
            name=TaskType.DOC_PARSE,
            handler=handler,
            resource=self._get_handler_resource(TaskType.DOC_PARSE),
        )
        logger.info("Registered doc_parse handler")

        # 注册 kg 索引 handler
        handler = KGIndexHandler(hyperrag_v2=self)
        TaskRegistry.register(
            name=TaskType.KG_INDEX,
            handler=handler,
            resource=self._get_handler_resource(TaskType.KG_INDEX),
        )
        logger.info("Registered kg index handler on kg_index task")
        wrapped_handler = getattr(handler.__call__, "__wrapped__", handler.__call__)
        logger.info(
            "KG index handler runtime target: file=%s firstlineno=%s qualname=%s",
            inspect.getsourcefile(wrapped_handler),
            wrapped_handler.__code__.co_firstlineno,
            wrapped_handler.__qualname__,
        )

        # 注册 bm25_index handler
        handler = BM25IndexHandler(
            bm25_storage_manager=self.bm25_storage_manager,
            hyperrag_v2=self,
        )
        TaskRegistry.register(
            name=TaskType.BM25_INDEX,
            handler=handler,
            resource=self._get_handler_resource(TaskType.BM25_INDEX),
        )
        logger.info("Registered bm25_index handler")

        # 注册 delete_asset handler
        handler = DeleteAssetHandler(
            bm25_storage_manager=self.bm25_storage_manager,
            artifact_store=self.artifact_store,
            hyperrag_v2=self,
        )
        TaskRegistry.register(
            name=TaskType.DELETE_ASSET,
            handler=handler,
            resource=self._get_handler_resource(TaskType.DELETE_ASSET),
        )
        logger.info("Registered delete_asset handler")
        TaskRegistry.register(
            name=TaskType.DELETE_INDEX,
            handler=DeleteIndexHandler(delete_asset_handler=handler),
            resource=self._get_handler_resource(TaskType.DELETE_INDEX),
        )
        logger.info("Registered delete_index handler")

        # 视频索引统一依赖 actio 解析。
        # 缺少 actio 依赖或模型配置时由 handler 在运行时快速失败。
        actio_config = {
            "models_config_path": self.config.actio_models_config_path,
            "segment_config": self.config.actio_segment_config_path,
            "aggregate_config": self.config.actio_aggregate_config_path,
            "merge_config": self.config.actio_merge_config_path,
            "device": self.config.actio_device,
            "asr_max_concurrency": self.config.actio_asr_max_concurrency,
            "annotate_max_concurrency": self.config.actio_annotate_max_concurrency,
            "keep_asr_segment_outputs": self.config.actio_keep_asr_segment_outputs,
            "keep_asr_audio_outputs": self.config.actio_keep_asr_audio_outputs,
        }
        handler = VideoParseHandler(
            artifact_store=self.artifact_store,
            max_asr_concurrent=self.config.actio_asr_max_concurrency,
            terminology_helper=self.terminology_helper,
            actio_config=actio_config,
        )
        TaskRegistry.register(
            name=TaskType.VIDEO_PARSE,
            handler=handler,
            resource=self._default_resource(slots=1, priority=5, gpu_memory_mb=1024),
        )
        logger.info("Registered actio-backed video_parse handler")
        TaskRegistry.register(
            name=TaskType.UPDATE_CAPTION,
            handler=handler,
            resource=self._default_resource(slots=1, priority=5, gpu_memory_mb=1024),
        )
        logger.info("Registered update_caption handler")

        # 注册 image_caption handler（如果配置了 vision_model_func）
        if callable(self.config.vision_model_func):
            handler = ImageCaptionHandler(
                artifact_store=self.artifact_store,
                vision_model_func=self.config.vision_model_func,
                max_concurrent_requests=self.config.vision_model_max_concurrent,
                terminology_helper=self.terminology_helper,
            )
            TaskRegistry.register(
                name=TaskType.IMAGE_CAPTION,
                handler=handler,
                resource=self._default_resource(slots=1, priority=5, gpu_memory_mb=0),
            )
            logger.info("Registered image_caption handler")

        # 注册 sop_video_parse handler（SOP 视频解析）。视频索引统一依赖 actio。
        # LLM 配置缺失时由 handler/CLI 在运行时给出错误。
        llm_base_url = getattr(self.config, "llm_api_base", None) or self.config.embedding_base_url

        llm_config = {
            "models_config_path": "",
            "base_url": llm_base_url,
            "api_key": self.config.llm_api_key,
        }
        embed_config = {
            "models_config_path": "",
            "base_url": llm_base_url,
            "api_key": self.config.llm_api_key,
        }
        sop_actio_config = {
            "models_config_path": self.config.actio_models_config_path,
            "segment_config": self.config.actio_segment_config_path,
            "aggregate_config": self.config.actio_aggregate_config_path,
            "merge_dtw_config": self.config.actio_merge_dtw_config_path,
            "sop_schema_path": self.config.actio_sop_schema_path,
            "device": self.config.actio_device,
            "asr_max_concurrency": self.config.actio_asr_max_concurrency,
            "annotate_max_concurrency": self.config.actio_annotate_max_concurrency,
            "keep_asr_segment_outputs": self.config.actio_keep_asr_segment_outputs,
            "keep_asr_audio_outputs": self.config.actio_keep_asr_audio_outputs,
        }

        handler = SOPVideoHandler(
            artifact_store=self.artifact_store,
            llm_config=llm_config,
            embed_config=embed_config,
            actio_config=sop_actio_config,
            max_asr_concurrent=self.config.actio_asr_max_concurrency,
        )
        TaskRegistry.register(
            name=TaskType.SOP_VIDEO_PARSE,
            handler=handler,
            resource=self._default_resource(slots=1, priority=5, gpu_memory_mb=2048),
        )
        logger.info("Registered sop_video_parse handler")

    def _get_handler_resource(self, task_type: str) -> TaskResource:
        """获取 handler 的资源需求"""
        # 默认资源需求
        defaults = {
            TaskType.DOC_PARSE: TaskResource(slots=1, priority=5),
            TaskType.KG_INDEX: TaskResource(slots=1, priority=5),
            TaskType.DELETE_ASSET: TaskResource(slots=1, priority=3),
            TaskType.DELETE_INDEX: TaskResource(slots=1, priority=3),
            TaskType.UPDATE_CAPTION: TaskResource(slots=1, priority=5, gpu_memory_mb=1024),
            TaskType.BM25_INDEX: TaskResource(slots=1, priority=5),
        }
        return defaults.get(task_type, TaskResource(slots=1, priority=5))

    def _compute_task_limits(self) -> dict[str, TaskLimitConfig]:
        """计算任务并发限制配置"""

        limits: dict[str, TaskLimitConfig] = {}

        # 从 config 获取配置
        if self.config.task_limits:
            for task_type, limit in self.config.task_limits.items():
                limits[task_type] = limit
        else:
            # 使用默认值
            limits = {
                "kg_index": TaskLimitConfig(
                    max_concurrent=self.config.max_index_workers,
                    gpu_memory_mb=0,
                ),
                "doc_parse": TaskLimitConfig(
                    max_concurrent=self.config.max_doc_parse_workers,
                    gpu_memory_mb=0,
                ),
                "video_parse": TaskLimitConfig(
                    max_concurrent=self.config.max_video_parse_workers,
                    gpu_memory_mb=0,
                ),
                "delete_asset": TaskLimitConfig(
                    max_concurrent=1,  # Serial deletion to avoid conflicts
                    gpu_memory_mb=0,
                ),
            }

        return limits

    # -------------------- workspace / storage helpers --------------------
    async def _get_kg_experiment(
        self,
        workspace: str,
        *,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
    ) -> SingleLayerKGService:
        await self._ensure_core_runtime()
        resolved_model = str(llm_model or self.config.llm_model or "")
        resolved_base_url = str(llm_base_url or self.config.llm_base_url or "")
        cache_key = (workspace, resolved_model, resolved_base_url)

        async with self._kg_experiments_lock:
            experiment = self._kg_experiments.get(cache_key)
            if experiment is not None:
                return experiment

            experiment_config = replace(
                self.config,
                workspace=workspace,
                llm_model=resolved_model,
                llm_base_url=resolved_base_url,
                llm_api_key=(llm_api_key or self.config.llm_api_key),
            )
            experiment = SingleLayerKGService(experiment_config)
            experiment.multimodal_embedder = self.multimodal_embedder
            await experiment.initialize()
            self._kg_experiments[cache_key] = experiment
            return experiment

    @staticmethod
    def _graph_source_id(chunk_ids: list[str]) -> str:
        normalized: list[str] = []
        seen: set[str] = set()
        for chunk_id in chunk_ids:
            value = str(chunk_id or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return "<SEP>".join(normalized)

    @staticmethod
    def _build_reference_list(chunks: list[dict[str, Any]]) -> list[dict[str, str]]:
        try:
            _, updated_chunks = generate_reference_list_from_chunks(chunks)
            reference_list: list[dict[str, str]] = []
            seen_paths: set[str] = set()
            for chunk in updated_chunks:
                file_path = str(chunk.get("file_path") or "")
                if not file_path or file_path == "unknown_source" or file_path in seen_paths:
                    continue
                seen_paths.add(file_path)
                reference_list.append(
                    {
                        "reference_id": str(len(reference_list) + 1),
                        "file_path": file_path,
                    }
                )
            return reference_list
        except Exception as e:
            logger.warning(f"Failed to build references from mix query chunks: {e}")
            return []

    async def aquery_kg(
        self,
        *,
        query: str,
        library_id: str,
        pre_compile: bool = False,
        top_k: int = 12,
        threshold: float = 0.0,
    ) -> dict[str, list[dict[str, str]]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"entities": [], "relations": []}

        precompiled_scope = str(
            getattr(self.config, "precompile_workspace", PRECOMPILED_WORKSPACE)
            or PRECOMPILED_WORKSPACE
        )
        scopes = [precompiled_scope] if pre_compile else [library_id, precompiled_scope]
        merged_entities: list[dict[str, str]] = []
        merged_relations: list[dict[str, str]] = []

        for scope in scopes:
            if not scope:
                continue
            experiment = await self._get_kg_experiment(scope)
            try:
                result = await experiment.query_workspace_graph(
                    query=normalized_query,
                    top_entities=max(1, int(top_k)),
                    top_relations=max(1, int(top_k)),
                    top_chunks=0,
                    use_llm_keywords=False,
                )
                entity_rows = [
                    item
                    for item in (result.get("entities") or [])
                    if float(item.get("score") or 0.0) >= float(threshold)
                ]
                relation_rows = [
                    item
                    for item in (result.get("relations") or [])
                    if float(item.get("score") or 0.0) >= float(threshold)
                ]
            except Exception as exc:
                logger.warning("aquery_kg query_workspace_graph fallback activated: %s", exc)
                entity_rows, relation_rows = await self._query_kg_repository_fallback(
                    experiment=experiment,
                    workspace=scope,
                    query=normalized_query,
                    top_k=max(1, int(top_k)),
                )
            normalized = normalize_kg_query_output(
                entities=entity_rows,
                relations=relation_rows,
                default_library_id=(
                    PRECOMPILED_LIBRARY_ID if scope == precompiled_scope else scope
                ),
            )
            merged_entities.extend(normalized.get("entities") or [])
            merged_relations.extend(normalized.get("relations") or [])

        entity_seen: set[tuple[str, str]] = set()
        entities: list[dict[str, str]] = []
        for item in merged_entities:
            key = (
                str(item.get("name") or "").strip().lower(),
                str(item.get("library_id") or "").strip(),
            )
            if not key[0] or key in entity_seen:
                continue
            entity_seen.add(key)
            entities.append(item)

        relation_seen: set[tuple[str, str]] = set()
        relations: list[dict[str, str]] = []
        for item in merged_relations:
            key = (
                str(item.get("description") or "").strip().lower(),
                str(item.get("library_id") or "").strip(),
            )
            if not key[0] or key in relation_seen:
                continue
            relation_seen.add(key)
            relations.append(item)

        limit = max(1, int(top_k))
        return {"entities": entities[:limit], "relations": relations[:limit]}

    async def aexport_knowledge_package(
        self,
        *,
        workspaces: list[str] | None = None,
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        precompiled_scope = str(
            getattr(self.config, "precompile_workspace", PRECOMPILED_WORKSPACE)
            or PRECOMPILED_WORKSPACE
        )
        experiment = await self._get_kg_experiment(precompiled_scope)
        return await experiment.export_precompiled_package(
            workspaces=workspaces,
            company_replacements=company_replacements,
            alias_fullname_map=alias_fullname_map,
        )

    async def aimport_knowledge_package(
        self,
        *,
        package: dict[str, Any],
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        precompiled_scope = str(
            getattr(self.config, "precompile_workspace", PRECOMPILED_WORKSPACE)
            or PRECOMPILED_WORKSPACE
        )
        experiment = await self._get_kg_experiment(precompiled_scope)
        return await experiment.import_precompiled_package(
            package=package,
            company_replacements=company_replacements,
            alias_fullname_map=alias_fullname_map,
        )

    def export_knowledge_package(self, *args, **kwargs) -> dict[str, Any]:
        return asyncio.run(self.aexport_knowledge_package(*args, **kwargs))

    def import_knowledge_package(self, *args, **kwargs) -> dict[str, Any]:
        return asyncio.run(self.aimport_knowledge_package(*args, **kwargs))

    async def _query_kg_repository_fallback(
        self,
        *,
        experiment: SingleLayerKGService,
        workspace: str,
        query: str,
        top_k: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        driver = getattr(experiment.repository, "driver", None)
        database = getattr(experiment.repository, "database", "neo4j")
        if driver is None:
            return [], []
        normalized_query = str(query or "").strip().lower()
        if not normalized_query:
            return [], []
        async with driver.session(database=database) as session:
            entity_result = await session.run(
                f"""
                MATCH (n:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE toLower(coalesce(n.entity_name, '')) CONTAINS $query_text
                   OR toLower(coalesce(n.entity_description, '')) CONTAINS $query_text
                RETURN n.node_id AS item_id,
                       n.entity_name AS entity_name,
                       n.entity_type AS entity_type,
                       n.entity_description AS entity_description,
                       n.file_path AS file_path,
                       coalesce(n.chunk_ids, []) AS chunk_ids,
                       1.0 AS score
                LIMIT $limit
                """,
                workspace=workspace,
                query_text=normalized_query,
                limit=top_k,
            )
            entities = [record.data() async for record in entity_result]

            relation_result = await session.run(
                f"""
                MATCH (src:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                      -[r:{SINGLE_LAYER_RELATION_TYPE} {{workspace: $workspace}}]->
                      (tgt:{SINGLE_LAYER_ENTITY_LABEL} {{workspace: $workspace}})
                WHERE toLower(coalesce(r.relationship_description, '')) CONTAINS $query_text
                   OR toLower(coalesce(src.entity_name, '')) CONTAINS $query_text
                   OR toLower(coalesce(tgt.entity_name, '')) CONTAINS $query_text
                RETURN r.relation_id AS relation_id,
                       src.entity_name AS source_entity,
                       tgt.entity_name AS target_entity,
                       r.relationship_description AS relationship_description,
                       r.file_path AS file_path,
                       coalesce(r.chunk_ids, []) AS chunk_ids,
                       1.0 AS score
                LIMIT $limit
                """,
                workspace=workspace,
                query_text=normalized_query,
                limit=top_k,
            )
            relations = [record.data() async for record in relation_result]
        return entities, relations

    def _fallback_chunk_from_mix_hit(self, chunk_hit: dict[str, Any]) -> dict[str, Any]:
        raw_metadata = (
            chunk_hit.get("metadata") if isinstance(chunk_hit.get("metadata"), dict) else {}
        )
        nested_metadata = (
            raw_metadata.get("metadata") if isinstance(raw_metadata.get("metadata"), dict) else {}
        )
        metadata = dict(nested_metadata)

        segment_ids = raw_metadata.get("segment_ids") or metadata.get("segment_ids") or []
        if segment_ids and "segment_ids" not in metadata:
            metadata["segment_ids"] = segment_ids
        if raw_metadata.get("order") is not None and "order" not in metadata:
            metadata["order"] = raw_metadata.get("order")
        if chunk_hit.get("file_path") and not metadata.get("file_path"):
            metadata["file_path"] = chunk_hit.get("file_path")
        if "edited" not in metadata:
            metadata["edited"] = False

        full_doc_id = str(
            metadata.get("doc_id") or metadata.get("asset_id") or metadata.get("video_id") or ""
        )
        return {
            "chunk_id": str(chunk_hit.get("chunk_id") or ""),
            "content": str(chunk_hit.get("text") or raw_metadata.get("text") or ""),
            "context": str(metadata.get("context") or ""),
            "tokens": int(raw_metadata.get("tokens") or 0),
            "full_doc_id": full_doc_id,
            "order": int(raw_metadata.get("order") or 0),
            "file_path": str(chunk_hit.get("file_path") or metadata.get("file_path") or ""),
            "score": chunk_hit.get("score"),
            "vector_score": chunk_hit.get("vector_score"),
            "metadata": metadata,
        }

    async def _get_workspace_asset_status_cache(
        self,
        workspace: str,
        *,
        refresh: bool = False,
    ) -> dict[str, AssetProcessingStatus]:
        async with self._workspace_asset_status_cache_lock:
            if not refresh:
                cached = self._workspace_asset_status_cache.get(workspace)
                if cached is not None:
                    return cached

        status_storage = get_asset_status_storage()
        status_map = await status_storage.get_all_status(workspace)

        async with self._workspace_asset_status_cache_lock:
            self._workspace_asset_status_cache[workspace] = status_map
        return status_map

    @staticmethod
    def _build_status_by_file_path(
        status_map: dict[str, AssetProcessingStatus],
    ) -> dict[str, AssetProcessingStatus]:
        out: dict[str, AssetProcessingStatus] = {}
        for status in status_map.values():
            file_path = str(getattr(status, "file_path", "") or "")
            if not file_path or file_path in out:
                continue
            out[file_path] = status
        return out

    @staticmethod
    def _resolve_status_for_file_path(
        file_path: str,
        status_by_file_path: dict[str, AssetProcessingStatus],
        status_map: dict[str, AssetProcessingStatus],
    ) -> AssetProcessingStatus | None:
        normalized_file_path = str(file_path or "").strip()
        if not normalized_file_path:
            return None

        direct_hit = status_by_file_path.get(normalized_file_path)
        if direct_hit is not None:
            return direct_hit

        try:
            resolved_path = str(Path(normalized_file_path).expanduser().resolve())
        except Exception:
            resolved_path = normalized_file_path
        if resolved_path != normalized_file_path:
            direct_hit = status_by_file_path.get(resolved_path)
            if direct_hit is not None:
                return direct_hit

        path_obj = Path(normalized_file_path)
        file_name = path_obj.name
        file_stem = path_obj.stem
        for status in status_map.values():
            candidate_path = str(getattr(status, "file_path", "") or "")
            if not candidate_path:
                continue
            candidate_obj = Path(candidate_path)
            if candidate_obj.name == file_name or candidate_obj.stem == file_stem:
                return status
        return None

    async def _hydrate_mix_chunks(
        self,
        workspace: str,
        raw_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        status_map = await self._get_workspace_asset_status_cache(workspace)
        status_by_file_path = self._build_status_by_file_path(status_map)

        detail_chunks: list[dict[str, Any]] = []
        for chunk in raw_chunks:
            detail_chunk = self._fallback_chunk_from_mix_hit(chunk)
            file_path = str(detail_chunk.get("file_path") or "")
            status = self._resolve_status_for_file_path(file_path, status_by_file_path, status_map)
            metadata = (
                detail_chunk.get("metadata")
                if isinstance(detail_chunk.get("metadata"), dict)
                else {}
            )
            if status is not None:
                detail_chunk["full_doc_id"] = str(
                    detail_chunk.get("full_doc_id") or status.asset_id
                )
                if not metadata.get("title"):
                    file_data = status.file_data if isinstance(status.file_data, dict) else {}
                    metadata["title"] = str(file_data.get("title") or "")
                if not metadata.get("file_path") and status.file_path:
                    metadata["file_path"] = str(status.file_path)
                if not metadata.get("asset_id"):
                    metadata["asset_id"] = str(status.asset_id)
            elif file_path and not detail_chunk.get("full_doc_id"):
                detail_chunk["full_doc_id"] = Path(file_path).stem
            detail_chunk["metadata"] = metadata
            detail_chunks.append(detail_chunk)
        return detail_chunks

    async def _normalize_kg_chunk_row(
        self,
        workspace: str,
        row: dict[str, Any],
        *,
        status_map: dict[str, AssetProcessingStatus] | None = None,
        status_by_file_path: dict[str, AssetProcessingStatus] | None = None,
    ) -> dict[str, Any]:
        resolved_status_map = (
            status_map
            if status_map is not None
            else await self._get_workspace_asset_status_cache(workspace)
        )
        resolved_status_by_file_path = (
            status_by_file_path
            if status_by_file_path is not None
            else self._build_status_by_file_path(resolved_status_map)
        )
        raw_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        nested_metadata = (
            raw_metadata.get("metadata") if isinstance(raw_metadata.get("metadata"), dict) else {}
        )
        metadata = dict(nested_metadata)
        segment_ids = raw_metadata.get("segment_ids") or metadata.get("segment_ids") or []
        if segment_ids and "segment_ids" not in metadata:
            metadata["segment_ids"] = segment_ids
        file_path = str(row.get("file_path") or metadata.get("file_path") or "")
        if file_path and not metadata.get("file_path"):
            metadata["file_path"] = file_path
        if raw_metadata.get("order") is not None and "order" not in metadata:
            metadata["order"] = raw_metadata.get("order")
        if "edited" not in metadata:
            metadata["edited"] = False

        status = self._resolve_status_for_file_path(
            file_path, resolved_status_by_file_path, resolved_status_map
        )
        if status is not None:
            metadata.setdefault("asset_id", str(status.asset_id))
            file_data = status.file_data if isinstance(status.file_data, dict) else {}
            if not metadata.get("title"):
                metadata["title"] = str(file_data.get("title") or "")
            full_doc_id = str(metadata.get("asset_id") or status.asset_id)
        else:
            full_doc_id = str(metadata.get("asset_id") or Path(file_path).stem if file_path else "")

        return {
            "chunk_id": str(row.get("item_id") or ""),
            "content": str(row.get("content") or raw_metadata.get("text") or ""),
            "context": str(metadata.get("context") or ""),
            "tokens": int(raw_metadata.get("tokens") or 0),
            "full_doc_id": full_doc_id,
            "order": int(raw_metadata.get("order") or 0),
            "file_path": file_path,
            "metadata": metadata,
        }

    async def _get_kg_chunks_by_ids(
        self,
        workspace: str,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        normalized_chunk_ids = [str(chunk_id) for chunk_id in chunk_ids if str(chunk_id).strip()]
        if not normalized_chunk_ids:
            return {}
        experiment = await self._get_kg_experiment(workspace)
        rows = await experiment._get_vector_store().get_chunks_by_ids(
            workspace=workspace,
            chunk_ids=normalized_chunk_ids,
        )
        if not rows:
            return {}
        status_map = await self._get_workspace_asset_status_cache(workspace)
        status_by_file_path = self._build_status_by_file_path(status_map)
        row_map = {
            str(row.get("item_id") or ""): row for row in rows if str(row.get("item_id") or "")
        }
        out: dict[str, dict[str, Any]] = {}
        for chunk_id in normalized_chunk_ids:
            row = row_map.get(chunk_id)
            if row is None:
                continue
            out[chunk_id] = await self._normalize_kg_chunk_row(
                workspace,
                row,
                status_map=status_map,
                status_by_file_path=status_by_file_path,
            )
        return out

    async def _get_kg_asset_chunks(
        self,
        workspace: str,
        file_id: str,
    ) -> list[dict[str, Any]]:
        status = await get_asset_status_storage().get_status(workspace, file_id)
        file_path = str(getattr(status, "file_path", "") or "")
        if not file_path:
            return []
        experiment = await self._get_kg_experiment(workspace)
        rows = await experiment._get_vector_store().get_chunks_by_file_path(
            workspace=workspace,
            file_path=index_storage_path(file_path),
        )
        if not rows:
            return []
        status_map = await self._get_workspace_asset_status_cache(workspace)
        status_by_file_path = self._build_status_by_file_path(status_map)
        chunks = [
            await self._normalize_kg_chunk_row(
                workspace,
                row,
                status_map=status_map,
                status_by_file_path=status_by_file_path,
            )
            for row in rows
        ]
        chunks.sort(key=lambda item: (int(item.get("order") or 0), str(item.get("chunk_id") or "")))
        return chunks

    async def _query_mix_compatible(
        self,
        workspace: str,
        query: str,
        *,
        chunk_top_k: int | None = None,
    ) -> dict[str, Any]:
        experiment = await self._get_kg_experiment(workspace)
        default_top_k = getattr(self.config, "vector_retrieval_top_k", None)
        if default_top_k is None:
            default_top_k = getattr(self.config, "kg_vector_chunk_top_k", 8)
        top_chunks = max(1, int(chunk_top_k or default_top_k or 8))
        logger.info(
            "Vector retrieval start: workspace=%s top_k=%s query=%s",
            workspace,
            top_chunks,
            query,
        )
        raw_result = await experiment.query_workspace(
            query=query,
            top_entities=max(1, int(self.config.kg_vector_entity_top_k or 10)),
            top_relations=max(1, int(self.config.kg_vector_relation_top_k or 12)),
            top_chunks=top_chunks,
            use_llm_keywords=False,
        )

        raw_chunks = list(raw_result.get("chunks", []) or [])
        chunk_ids = [
            str(chunk.get("chunk_id") or "")
            for chunk in raw_chunks
            if str(chunk.get("chunk_id") or "")
        ]
        detail_chunks = await self._hydrate_mix_chunks(workspace, raw_chunks)
        logger.info(
            "Vector retrieval done: workspace=%s top_k=%s query=%s "
            "raw_chunks=%s hydrated_chunks=%s",
            workspace,
            top_chunks,
            query,
            len(raw_chunks),
            len(detail_chunks),
        )

        entities = []
        for entity in raw_result.get("entities", []) or []:
            source_chunk_ids = [
                str(item) for item in entity.get("chunk_ids") or [] if str(item).strip()
            ]
            entities.append(
                {
                    "entity_name": str(entity.get("entity_name") or ""),
                    "entity_type": str(entity.get("entity_type") or "Concept"),
                    "description": str(entity.get("entity_description") or ""),
                    "source_id": self._graph_source_id(source_chunk_ids),
                    "file_path": str(entity.get("file_path") or ""),
                    "weight": float(entity.get("vector_score") or 0.0),
                }
            )

        relationships = []
        for relation in raw_result.get("relations", []) or []:
            source_chunk_ids = [
                str(item) for item in relation.get("chunk_ids") or [] if str(item).strip()
            ]
            relationships.append(
                {
                    "src_id": str(relation.get("source_entity") or ""),
                    "tgt_id": str(relation.get("target_entity") or ""),
                    "description": str(relation.get("relationship_description") or ""),
                    "weight": float(relation.get("vector_score") or 0.0),
                    "source_id": self._graph_source_id(source_chunk_ids),
                    "file_path": str(relation.get("file_path") or ""),
                    "keywords": relation.get("relation_keywords") or [],
                    "situation": str(relation.get("situation") or ""),
                }
            )

        references = self._build_reference_list(detail_chunks)
        langfuse_trace = (
            raw_result.get("langfuse_trace", {}) if isinstance(raw_result, dict) else {}
        )
        if not isinstance(langfuse_trace, dict):
            langfuse_trace = {}
        if not langfuse_trace:
            chunk_ids = [
                str(chunk.get("chunk_id") or "")
                for chunk in raw_chunks
                if str(chunk.get("chunk_id") or "")
            ]
            chunk_entries = [
                f"{chunk['chunk_id']}|{chunk.get('text', '')}"
                for chunk in raw_chunks
                if chunk.get("chunk_id")
            ]
            langfuse_trace = {
                "pre_topk_chunk_ids": chunk_ids,
                "output_data": {
                    "vector_chunks": chunk_entries,
                    "entity_chunks": [
                        f"{chunk['chunk_id']}|{chunk.get('text', '')}"
                        for chunk in raw_chunks
                        if chunk.get("chunk_id") and chunk.get("entity_hits")
                    ],
                    "relation_chunks": [
                        f"{chunk['chunk_id']}|{chunk.get('text', '')}"
                        for chunk in raw_chunks
                        if chunk.get("chunk_id") and chunk.get("relation_hits")
                    ],
                    "merged_chunks": chunk_entries,
                },
                "chunk_ids": chunk_ids,
                "chunks_with_ids": chunk_entries,
                "bm25_chunk_ids": [],
                "bm25_chunks_with_ids": [],
                "combine_chunks_with_ids": chunk_entries,
            }
        langfuse_trace["mix_query_stats"] = raw_result.get("stats", {})

        return {
            "status": "success",
            "data": {
                "chunks": detail_chunks,
                "entities": entities,
                "relationships": relationships,
                "references": references,
            },
            "langfuse_trace": langfuse_trace,
        }

    @staticmethod
    def _has_chunk_detail_metadata(metadata: Any) -> bool:
        if not isinstance(metadata, dict):
            return False
        if "chunk_type" in metadata:
            return True
        return any(k in metadata for k in ("locations", "begin_ts", "end_ts", "image_url"))

    @staticmethod
    def _require_base64_image_data_url(image_url: str) -> str:
        normalized = str(image_url or "").strip()
        lowered = normalized.lower()
        if not lowered.startswith("data:image/"):
            raise ValueError("image_url must be a data:image/*;base64,... URL")
        if ";base64," not in lowered:
            raise ValueError("image_url must contain base64 payload")
        return normalized

    def _get_workspace_cache_dirs(self, workspace: str) -> tuple[Path, Path]:
        """通过 ArtifactStore 获取 workspace 的 cache 目录"""
        if self.artifact_store is None:
            # 回退逻辑（不应该发生，但为了安全）
            working_dir = Path(self.config.working_dir)
            workspace_dir = working_dir / (workspace or "default")
            mineru_cache_dir = Path(
                self.config.mineru_cache_dir or (workspace_dir / "mineru_cache")
            )
            video_cache_dir = Path(self.config.video_cache_dir or (workspace_dir / "video_cache"))
            return mineru_cache_dir, video_cache_dir

        return (
            self.artifact_store.get_mineru_cache_root(workspace),
            self.artifact_store.get_video_cache_root(workspace),
        )

    @staticmethod
    def _default_resource(slots: int = 1, priority: int = 5, gpu_memory_mb: int = 0):
        # 延迟 import，避免循环依赖
        from .task.models import TaskResource

        return TaskResource(slots=slots, priority=priority, gpu_memory_mb=gpu_memory_mb)

    @staticmethod
    def _normalize_str_ci(s: Any) -> str:
        """字符串大小写归一化（用于不区分大小写匹配）"""
        if not isinstance(s, str):
            return str(s)
        return s.lower()

    @staticmethod
    def _match_value(meta_val: Any, cond_val: Any) -> bool:
        """
        匹配 metadata 值与条件值
        - 支持标量值匹配
        - 支持列表值匹配（cond_val 是列表时，meta_val 只要等于列表中任一值即可）
        - 字符串不区分大小写
        """
        if cond_val is None:
            return meta_val is None

        # 如果条件值是列表，检查 meta_val 是否在列表中
        if isinstance(cond_val, list):
            if not cond_val:
                return False
            for cv in cond_val:
                if HyperRAGV2._match_value(meta_val, cv):
                    return True
            return False

        # 字符串不区分大小写匹配
        if isinstance(meta_val, str) and isinstance(cond_val, str):
            return HyperRAGV2._normalize_str_ci(meta_val) == HyperRAGV2._normalize_str_ci(cond_val)

        # 其他类型严格相等
        return meta_val == cond_val

    @staticmethod
    def _eval_where(expr: dict[str, Any], attributes: dict[str, Any] | None) -> bool:
        """
        递归解析 where 表达式并评估是否匹配 attributes

        Args:
            expr: where 表达式（支持 AND/OR 嵌套，或叶子条件 {key: value}）
            attributes: 文档的 attributes 字典

        Returns:
            True 表示匹配，False 表示不匹配
        """
        if not expr:
            return True

        if not isinstance(expr, dict):
            return True  # 非 dict 类型的 where 表达式视为匹配（兼容性考虑）

        # 处理 AND 操作
        if "AND" in expr:
            and_list = expr["AND"]
            if not isinstance(and_list, list):
                return False
            for sub_expr in and_list:
                if not HyperRAGV2._eval_where(sub_expr, attributes):
                    return False
            return True

        # 处理 OR 操作
        if "OR" in expr:
            or_list = expr["OR"]
            if not isinstance(or_list, list):
                return False
            for sub_expr in or_list:
                if HyperRAGV2._eval_where(sub_expr, attributes):
                    return True
            return False

        # 叶子条件：{key: value} 或 {key: [value1, value2]}
        # 如果 attributes 中缺少 key，视为满足（用户已确认）
        for key, cond_val in expr.items():
            if attributes is None or not attributes:
                return True  # 缺失 attributes 视为满足
            meta_val = attributes.get(key)
            if meta_val is None or not meta_val:
                return True  # 缺失 key 视为满足
            if not HyperRAGV2._match_value(meta_val, cond_val):
                return False

        return True

    @staticmethod
    def _inject_title_into_chunks(
        chunks: list[dict[str, Any]],
        status_map: dict[str, Any],
    ) -> None:
        """就地为 chunk list 的 metadata 注入 title 字段（无法获取时注入空字符串）"""
        for chunk in chunks:
            metadata = chunk.get("metadata")
            if metadata is None:
                continue
            if "title" in metadata:
                continue
            doc_id = chunk.get("full_doc_id", "")
            title = ""
            status = status_map.get(doc_id) if doc_id else None
            if status is not None:
                try:
                    fd = status.file_data or {}
                    title = fd.get("title") or ""
                except Exception:
                    pass
            metadata["title"] = title

    async def _filter_query_data_by_where(
        self, workspace: str, query_data: dict[str, Any], where: dict[str, Any]
    ) -> dict[str, Any]:
        """
        根据 where 条件过滤 query_data 结果

        Args:
            workspace: 工作空间名称
            query_data: KG 查询返回的结果
            where: where 条件表达式

        Returns:
            过滤后的 query_data（保持相同结构）
        """
        # 如果结果不是 success 或没有 data，直接返回
        if query_data.get("status") != "success" or "data" not in query_data:
            return query_data

        data = query_data["data"]
        chunks = data.get("chunks", [])
        if not chunks:
            return query_data

        chunk_id_to_doc_id: dict[str, str] = {}
        for chunk_data in chunks:
            if not chunk_data:
                continue
            cid = chunk_data.get("chunk_id")
            doc_id = chunk_data.get("full_doc_id")
            if cid and doc_id:
                chunk_id_to_doc_id[str(cid)] = str(doc_id)

        # 评估 where 条件（基于文档 file_data），得到保留的 chunk_id 集合
        # 兼容旧数据：如果文档 file_data 不存在，则不过滤（保留该chunk）
        doc_to_chunks: dict[str, list[dict[str, Any]]] = {}
        no_doc_chunks: list[dict[str, Any]] = []

        for chunk in chunks:
            cid = chunk.get("chunk_id")
            doc_id = chunk_id_to_doc_id.get(str(cid)) if cid else None
            if not doc_id:
                no_doc_chunks.append(chunk)
                continue
            if doc_id not in doc_to_chunks:
                doc_to_chunks[doc_id] = []
            doc_to_chunks[doc_id].append(chunk)

        doc_ids = list(doc_to_chunks.keys())
        keep_doc_ids: set[str] = set()
        if doc_ids:
            st = get_asset_status_storage()
            status_map = await st.get_status_list(workspace, doc_ids)
            for doc_id in doc_ids:
                status = status_map.get(doc_id)
                file_data = status.file_data if status is not None else None
                attributes = file_data.get("attributes") if file_data is not None else None
                if attributes is None or HyperRAGV2._eval_where(where, attributes):
                    keep_doc_ids.add(doc_id)

        filtered_chunks: list[dict[str, Any]] = []
        for keep_id in keep_doc_ids:
            filtered_chunks.extend(doc_to_chunks.get(keep_id, []))
        filtered_chunks.extend(no_doc_chunks)
        # 注入 title 到 chunk metadata（复用已有 status_map，零额外 I/O）
        HyperRAGV2._inject_title_into_chunks(filtered_chunks, status_map)

        keep_chunk_ids = {c.get("chunk_id") for c in filtered_chunks if c.get("chunk_id")}

        # 过滤 entities 和 relationships（基于 source_id）
        # source_id 格式：chunk_id1<SEP>chunk_id2<SEP>...
        GRAPH_FIELD_SEP = "<SEP>"
        filtered_entities = []
        for entity in data.get("entities", []):
            source_id = entity.get("source_id", "")
            if source_id:
                source_chunk_ids = source_id.split(GRAPH_FIELD_SEP)
                if any(cid in keep_chunk_ids for cid in source_chunk_ids):
                    filtered_entities.append(entity)
            else:
                # 如果没有 source_id，保留（保守策略）
                filtered_entities.append(entity)

        filtered_relationships = []
        for rel in data.get("relationships", []):
            source_id = rel.get("source_id", "")
            if source_id:
                source_chunk_ids = source_id.split(GRAPH_FIELD_SEP)
                if any(cid in keep_chunk_ids for cid in source_chunk_ids):
                    filtered_relationships.append(rel)
            else:
                # 如果没有 source_id，保留（保守策略）
                filtered_relationships.append(rel)

        # 重新生成 references（基于过滤后的 chunks）
        try:
            _, updated_chunks = generate_reference_list_from_chunks(filtered_chunks)
            # 从 updated_chunks 中提取 references
            file_path_to_ref_id = {}
            reference_list = []
            seen_paths = set()
            for i, chunk in enumerate(updated_chunks):
                file_path = chunk.get("file_path", "")
                if file_path and file_path != "unknown_source" and file_path not in seen_paths:
                    ref_id = str(len(reference_list) + 1)
                    file_path_to_ref_id[file_path] = ref_id
                    reference_list.append({"reference_id": ref_id, "file_path": file_path})
                    seen_paths.add(file_path)
        except Exception as e:
            logger.warning(f"Failed to regenerate references: {e}, using original references")
            reference_list = data.get("references", [])

        # 构建过滤后的结果
        filtered_data = {
            "entities": filtered_entities,
            "relationships": filtered_relationships,
            "chunks": filtered_chunks,
            "references": reference_list,
        }

        return {
            **query_data,
            "data": filtered_data,
        }

    # -------------------- 回调：写入 AssetStatusStorage --------------------
    @staticmethod
    def _build_asset_metadata(
        asset: Any,
        *,
        parse_mode: str | None = None,
        semantic: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = asset if isinstance(asset, dict) else {}
        extra = payload.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        modality = str(payload.get("modality") or "").strip().lower()
        doc_type = str(payload.get("doc_type") or "").strip().lower()
        is_video = (
            modality == "video"
            or doc_type in {"video", "sop_video"}
            or parse_mode in {"video", "sop_video"}
        )
        if is_video:
            metadata: dict[str, Any] = {"asset_type": "video"}
            semantic_metadata = semantic.get("metadata", {}) if isinstance(semantic, dict) else {}
            semantic_metadata = semantic_metadata if isinstance(semantic_metadata, dict) else {}
            # Task completion is distinct from caption completeness. Copy successful
            # reruns too, so merged asset metadata does not retain an old failure.
            for key in ("caption_stats", "indexing_outcome", "global_context_quality_warnings"):
                if key in extra:
                    metadata[key] = extra[key]
                elif key in semantic_metadata:
                    metadata[key] = semantic_metadata[key]
            duration = payload.get("video_duration")
            if duration is None:
                duration = payload.get("duration_sec")
            if duration is None:
                duration = extra.get("duration_sec")

            if duration is None and isinstance(semantic, dict):
                video_metadata = semantic.get("video_metadata")
                if not isinstance(video_metadata, dict):
                    semantic_metadata = semantic.get("metadata")
                    video_metadata = (
                        semantic_metadata.get("video_info")
                        if isinstance(semantic_metadata, dict)
                        else None
                    )
                if isinstance(video_metadata, dict):
                    duration = video_metadata.get("duration_sec")
                if duration is None and isinstance(semantic.get("metadata"), dict):
                    duration = semantic["metadata"].get("duration_sec")

            if duration is not None:
                with contextlib.suppress(TypeError, ValueError):
                    metadata["video_duration"] = float(duration)
            return metadata

        metadata = {"asset_type": "doc"}
        page_info = payload.get("page_info")
        page_info = page_info if isinstance(page_info, dict) else {}
        page_count = payload.get("page_count")
        if page_count is None:
            page_count = page_info.get("total_pages")
        if page_count is None:
            page_count = extra.get("total_pages")
        if page_count is not None:
            with contextlib.suppress(TypeError, ValueError):
                metadata["page_count"] = int(page_count)
        return metadata

    @classmethod
    def _build_task_asset_metadata(cls, task: Any) -> dict[str, Any]:
        task_result = getattr(task, "result", None)
        task_result = task_result if isinstance(task_result, dict) else {}
        task_metadata = getattr(task, "metadata", None)
        task_metadata = task_metadata if isinstance(task_metadata, dict) else {}
        asset = task_result.get("asset")
        if not isinstance(asset, dict):
            asset = task_metadata.get("asset")
        semantic = task_result.get("sop_full")
        semantic = semantic if isinstance(semantic, dict) else None
        parse_mode = {
            TaskType.SOP_VIDEO_PARSE: "sop_video",
            TaskType.VIDEO_PARSE: "video",
            TaskType.UPDATE_CAPTION: "video",
            TaskType.DOC_PARSE: "document",
        }.get(task.task_type)
        return cls._build_asset_metadata(
            asset,
            parse_mode=parse_mode,
            semantic=semantic,
        )

    @staticmethod
    def _get_task_chunk_count(task: Any) -> int | None:
        task_result = getattr(task, "result", None)
        task_result = task_result if isinstance(task_result, dict) else {}
        counters = task_result.get("counters")
        value = counters.get("chunks") if isinstance(counters, dict) else None
        if value is None:
            value = task_result.get("chunk_count")
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def _persist_task_asset_metadata(
        self,
        task: Any,
        *,
        include_chunk_count: bool = False,
    ) -> None:
        workspace = task.workspace or "default"
        asset_id = task.asset_id
        storage = get_asset_status_storage()
        metadata = self._build_task_asset_metadata(task)
        if include_chunk_count:
            chunk_count = self._get_task_chunk_count(task)
            if chunk_count is None:
                try:
                    chunk_count = await self.aget_chunk_count(workspace, asset_id)
                except Exception:
                    logger.exception(
                        "Failed to derive chunk count for workspace=%s asset_id=%s",
                        workspace,
                        asset_id,
                    )
            if chunk_count is not None:
                metadata["chunk_nums"] = chunk_count

        if not metadata:
            fallback = await self.aget_asset_metadata(workspace, asset_id)
            if not include_chunk_count:
                return
            metadata = fallback or {}
        if not metadata:
            return

        try:
            status = await storage.get_status(workspace, asset_id)
            stored_metadata = (
                dict(status.metadata)
                if status is not None and isinstance(status.metadata, dict)
                else {}
            )
        except Exception:
            logger.exception(
                "Failed to read current asset metadata before update for workspace=%s asset_id=%s",
                workspace,
                asset_id,
            )
            stored_metadata = {}

        await self._persist_asset_metadata_updates(
            storage,
            workspace=workspace,
            asset_id=asset_id,
            stored_metadata=stored_metadata,
            metadata=metadata,
            remove_keys=["doc_type"],
        )

    async def _on_job_callback(self, job, prev_status, new_status):
        try:
            workspace = job.workspace or "default"
            asset_id = job.asset_id
            if asset_id:
                st = get_asset_status_storage()
                job_name = job.job_name

                if str(new_status) == "completed":
                    # 删除 job 完成时不覆盖状态（已由 task callback 设置为 DELETED）
                    if job_name != "delete":
                        await st.set_status(
                            workspace, asset_id, AssetStatus.COMPLETED, progress=1.0
                        )
                        if bool(job.metadata.get("pre_compile", False)):
                            logger.info(
                                "Skip knowledgebase summary for precompile job: "
                                "workspace=%s asset=%s",
                                workspace,
                                asset_id,
                            )
                            return
                        summary_inputs = await self._resolve_job_summary_inputs(job)
                        knowledgebase_info = await self.async_knowledgebase_summary(
                            workspace,
                            asset_id=asset_id,
                            asset_title=summary_inputs["asset_title"],
                            document_summary=summary_inputs["document_summary"],
                        )
                        logger.info(
                            "Job completed knowledgebase summary updated: workspace=%s asset=%s summary=%s",
                            workspace,
                            asset_id,
                            knowledgebase_info.summarization,
                        )
                elif str(new_status) == "failed":
                    await st.set_status(
                        workspace,
                        asset_id,
                        AssetStatus.FAILED,
                        error_message=job.error_message,
                    )
        except Exception:
            logger.exception("HyperRAGV2 job status update failed; ignored")
        finally:
            if self._user_on_job_callback:
                if asyncio.iscoroutinefunction(self._user_on_job_callback):
                    await self._user_on_job_callback(job, prev_status, new_status)
                else:
                    await asyncio.to_thread(
                        self._user_on_job_callback, job, prev_status, new_status
                    )

    async def _resolve_job_summary_inputs(self, job: JobInstance) -> dict[str, str | None]:
        document_summary: str | None = None
        asset_title: str | None = None
        if self._queue is None:
            return {"document_summary": document_summary, "asset_title": asset_title}

        task_ids = list(getattr(job, "task_ids", []) or [])
        for task_id in reversed(task_ids):
            task = await self._queue.get_task(task_id)
            if task is None:
                continue
            if task.task_type != TaskType.KG_INDEX:
                continue

            result_payload = task.result if isinstance(task.result, dict) else {}
            metadata_payload = task.metadata if isinstance(task.metadata, dict) else {}
            document_summary = normalize_text(result_payload.get("document_summary")) or None

            asset_payload = (
                result_payload.get("asset") if isinstance(result_payload.get("asset"), dict) else {}
            )
            if not asset_payload:
                asset_payload = (
                    metadata_payload.get("asset")
                    if isinstance(metadata_payload.get("asset"), dict)
                    else {}
                )
            asset_title = normalize_text(asset_payload.get("title")) or None
            break

        return {"document_summary": document_summary, "asset_title": asset_title}

    async def _on_task_callback(self, task: Task, prev_status, new_status):
        # TODO 要把所有状态转换的逻辑理一下
        try:
            workspace = task.workspace or "default"
            asset_id = task.asset_id
            if asset_id:
                st = get_asset_status_storage()
                status_str = str(new_status)

                if task.task_type == TaskType.VIDEO_PARSE and status_str in {
                    TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value
                }:
                    # A fresh parse/success must not display a previous run's timeout.
                    await st.update_metadata(workspace, asset_id, {}, remove_keys=["video_split"])

                # 失败优先
                if status_str == TaskStatus.FAILED.value:
                    timeout_details = (task.result or {}).get("video_split")
                    await st.set_status(
                        workspace,
                        asset_id,
                        AssetStatus.FAILED,
                        error_message=task.error_message,
                        metadata={"video_split": timeout_details}
                        if isinstance(timeout_details, dict)
                        else None,
                    )
                elif status_str == TaskStatus.COMPLETED.value:
                    # 处理删除任务完成
                    if task.task_type in {TaskType.DELETE_ASSET, TaskType.DELETE_INDEX}:
                        job_name = task.job_name or "delete"
                        # 对于单纯的删除任务，直接标记为 DELETED
                        if job_name == "delete" and task.task_type in {
                            TaskType.DELETE_ASSET,
                            TaskType.DELETE_INDEX,
                        }:
                            await st.set_status(workspace, asset_id, AssetStatus.DELETED)
                            logger.info(f"Asset {asset_id} marked as DELETED")
                        # 文档、术语任务的删除会直接转到索引中状态
                        elif job_name == "document" or job_name == "terminology":
                            await st.set_status(
                                workspace, asset_id, AssetStatus.INDEXING, progress=0.5
                            )
                        elif job_name in {"video", "sop_video", "video_update"}:
                            await st.set_status(
                                workspace, asset_id, AssetStatus.INDEXING, progress=0.9
                            )
                    elif task.task_type in {
                        TaskType.DOC_PARSE,
                        TaskType.VIDEO_PARSE,
                        TaskType.SOP_VIDEO_PARSE,
                        TaskType.UPDATE_CAPTION,
                    }:
                        await self._persist_task_asset_metadata(task)
                    elif task.task_type == TaskType.KG_INDEX:
                        await self._persist_task_asset_metadata(task, include_chunk_count=True)
                elif status_str in {TaskStatus.RUNNING.value, TaskStatus.QUEUED.value}:
                    asset_path = None
                    try:
                        asset_path = (task.metadata or {}).get("asset_path")
                    except Exception:
                        asset_path = None

                    # 直接使用 task.task_type 枚举进行比较，不要用字符串
                    if task.task_type in {
                        TaskType.DOC_PARSE,
                        TaskType.VIDEO_PARSE,
                        TaskType.SOP_VIDEO_PARSE,
                        TaskType.UPDATE_CAPTION,
                    }:
                        if task.task_type == TaskType.SOP_VIDEO_PARSE:
                            parse_mode = "sop_video"
                        else:
                            parse_mode = (
                                "video"
                                if task.task_type in {TaskType.VIDEO_PARSE, TaskType.UPDATE_CAPTION}
                                else "document"
                            )
                        await st.set_status(
                            workspace,
                            asset_id,
                            AssetStatus.PARSING,
                            progress=0.1,
                            error_message="",
                            parse_mode=parse_mode,
                            file_path=asset_path,
                        )
                    elif task.task_type in {TaskType.IMAGE_CAPTION, TaskType.CHUNKING}:
                        await st.set_status(
                            workspace,
                            asset_id,
                            AssetStatus.CHUNKING,
                            progress=0.2,
                            error_message="",
                        )
                    elif task.task_type == TaskType.KG_INDEX:
                        await st.set_status(
                            workspace,
                            asset_id,
                            AssetStatus.INDEXING,
                            progress=0.5,
                            error_message="",
                        )
        except Exception:
            logger.exception("HyperRAGV2 task status update failed; ignored")
        finally:
            if self._user_on_task_callback:
                if asyncio.iscoroutinefunction(self._user_on_task_callback):
                    await self._user_on_task_callback(task, prev_status, new_status)
                else:
                    await asyncio.to_thread(
                        self._user_on_task_callback, task, prev_status, new_status
                    )

    # -------------------- 对外：任务/查询 --------------------
    async def aadd_asset(
        self,
        workspace: str,
        asset_id: str,
        asset_path: str,
        params: dict | None = None,
        on_complete: str | None = None,
        on_fail: str | None = None,
        request_id: str | None = None,
        fast_ingest: bool = False,
        pre_compile: bool = False,
    ) -> str:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        assert self._queue is not None
        st = get_asset_status_storage()
        if fast_ingest and pre_compile:
            raise ValueError("fast_ingest and pre_compile cannot be enabled together")

        # skip re-indexing if the asset is already in a processing state
        existing_status = await st.get_status(workspace, asset_id)
        normalized_asset_path = str(asset_path)
        if (
            existing_status is not None
            and existing_status.status
            in [
                AssetStatus.PARSING,
                AssetStatus.CHUNKING,
                AssetStatus.INDEXING,
                AssetStatus.QUEUED,
                AssetStatus.POST_PROCESSING,
            ]
            and str(existing_status.file_path or "") == normalized_asset_path
        ):
            logger.info(
                "Skip re-indexing processing asset: workspace=%s asset_id=%s file_path=%s",
                workspace,
                asset_id,
                normalized_asset_path,
            )
            return f"job-skip-{asset_id}"

        # 预写入状态（与 hyperrag.py 对齐：queued + file_path + parse_mode）
        ext = Path(asset_path).suffix.lower()
        params_dict = dict(params or {})
        params_dict["fast_ingest"] = bool(fast_ingest or params_dict.get("fast_ingest"))
        params_dict["pre_compile"] = bool(pre_compile or params_dict.get("pre_compile"))
        if params_dict["pre_compile"]:
            precompiled_workspace = str(self.config.precompile_workspace or PRECOMPILED_WORKSPACE)
            await self.adelete_knowledgebase_info(precompiled_workspace)
        if params_dict["pre_compile"] and existing_status is not None:
            previous_failures = (
                existing_status.metadata.get("precompile_failed_er_chunks")
                if isinstance(existing_status.metadata, dict)
                else None
            )
            if isinstance(previous_failures, list):
                retry_er_chunk_ids = [
                    str(item["chunk_id"])
                    for item in previous_failures
                    if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
                ]
                if retry_er_chunk_ids:
                    params_dict["retry_er_chunk_ids"] = retry_er_chunk_ids

        # 检查是否使用 SOP 视频解析
        parser_type = params_dict.get("parser_type")
        if parser_type == "sop_video":
            if TaskRegistry.get(TaskType.SOP_VIDEO_PARSE) is None:
                raise ValueError(
                    "SOP video parse requested but sop_video_parse task is not registered. "
                    "Please ensure actio is installed and HyperRAG task handlers are initialized."
                )
            parse_mode = "sop_video"
        else:
            parse_mode = (
                "video" if ext in {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"} else "document"
            )
        #
        if parse_mode == "document":
            if "backend" not in params_dict and self.config.mineru_backend:
                params_dict["backend"] = self.config.mineru_backend

        previous_file_path = ""
        if existing_status is not None:
            previous_file_path = str(existing_status.file_path or "").strip()
        if previous_file_path and previous_file_path != normalized_asset_path:
            params_dict["previous_file_path"] = previous_file_path

        # 直接使用 params 作为 file_data 保存，不把仅供删除使用的旧路径写入状态。
        file_data = dict(params_dict) if params_dict else None
        if file_data is not None:
            file_data.pop("previous_file_path", None)

        # metadata 仅用于派生信息（页数/时长等），不包含原始 file_data
        status_metadata: dict[str, Any] = {}

        await st.set_status(
            workspace,
            asset_id,
            AssetStatus.QUEUED,
            progress=0.0,
            parse_mode=parse_mode,
            file_path=normalized_asset_path,
            metadata=status_metadata if status_metadata else None,
            file_data=file_data,
        )

        callbacks: dict[str, str] = {}
        if on_complete:
            callbacks["on_complete"] = on_complete
        if on_fail:
            callbacks["on_fail"] = on_fail

        job, tasks = JobBuilder.from_asset(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=asset_path,
            params=params_dict,  # 使用合并后的 params_dict
            callbacks=callbacks,
            request_id=request_id,
        )
        job_id = await self.scheduler.submit_job(job, tasks)

        # 记录 job_id 映射，用于后续 cancel
        job_by_asset_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"
        try:
            await self._queue.redis.set(job_by_asset_key, job_id, ex=86400)  # 24小时过期
        except Exception as e:
            logger.warning(f"Failed to record job_by_asset mapping: {e}")

        return job_id

    def add_asset(self, *args, **kwargs) -> str:
        return asyncio.run(self.aadd_asset(*args, **kwargs))

    async def aquery(self, workspace: str, question: str, mode: str = "hybrid") -> str:
        result = await self.aquery_data(
            workspace=workspace,
            query=question,
            mode=mode,
            retrieval_mode="hybrid",
        )
        return json.dumps(result, ensure_ascii=False)

    def query(self, *args, **kwargs) -> str:
        return asyncio.run(self.aquery(*args, **kwargs))

    def _jieba_tokenize(self, text: str, *, strict: bool = False) -> str:
        """
        使用 jieba 对文本进行分词
        参考 nexus-v 的 _bm25_tokenizer 方法，统一使用 cut_for_search

        Args:
            text: 待分词文本

        Returns:
            空格分隔的分词结果字符串
        """
        if not text:
            return ""

        # 1. 预处理：移除 [xxx] 格式的元数据标签
        text = re.sub(r"\[.*?\]", " ", text)

        # 2. 中英文/数字分离处理
        tokens = []
        try:
            for part in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9\-\.\/\&\+\_]+", text):
                if re.match(r"[\u4e00-\u9fff]+", part):
                    # 中文部分用 cut_for_search
                    tokens.extend(jieba.cut_for_search(part, HMM=self.config.bm25_jieba_hmm))
                else:
                    # 英文/数字/型号保持原样（转小写）
                    tokens.append(part.lower())

            # 3. 过滤空字符串和纯标点
            tokens = [t.strip() for t in tokens if t.strip() and not re.fullmatch(r"[^\w]", t)]

            # 4. 停用词过滤
            if self.bm25_stopwords:
                tokens = [t for t in tokens if t not in self.bm25_stopwords]
        except Exception as e:
            if strict:
                raise
            logger.warning(f"jieba tokenize failed : '{e}'")

        return " ".join(tokens)

    def _get_default_stopwords(self) -> set[str]:
        """获取默认停用词（参考 nexus-v）"""
        return {
            # 中文停用词
            "的",
            "了",
            "在",
            "是",
            "有",
            "和",
            "与",
            "也",
            "其",
            "此",
            "该",
            "等",
            "呢",
            "其中",
            "如下",
            "什么",
            "图",
            "表",
            "图示",
            "步骤",
            "字幕",
            "视觉要点",
            "参见",
            "内容",
            "如果",
            # 英文停用词
            "the",
            "a",
            "an",
            "in",
            "for",
            "on",
            "by",
            "as",
            "at",
            "from",
            "to",
            "or",
            "do",
            "does",
            "did",
            "but",
            "if",
            "because",
            "so",
            "such",
            "of",
            "and",
            "is",
            "are",
            "was",
            "were",
            # 领域通用停用词
            "mean",
            "respectively",
            "step",
            "figure",
            "fig",
            "table",
            "caption",
            "see",
            "what",
            "which",
            "where",
            "when",
            "why",
            "that",
            "this",
            "these",
            "those",
            "then",
            "there",
            "through",
            "throughout",
            "toward",
            "towards",
            "underneath",
            "until",
            "up",
            "upon",
            "us",
            "use",
            "using",
            "we",
            "well",
            "who",
            "whom",
            "with",
            "within",
            "without",
            "would",
            "you",
            "your",
            "yours",
            "yourself",
            "yourselves",
        }

    async def _load_bm25_stopwords(self):
        """加载 BM25 停用词"""
        stop_file = self.config.bm25_stopwords_file
        if not stop_file:
            # 使用内置默认停用词
            self.bm25_stopwords = self._get_default_stopwords()
            logger.debug("Using default BM25 stopwords")
            return

        stop_path = Path(stop_file)
        if not stop_path.exists():
            logger.warning(f"BM25 stopwords file not found: {stop_path}, using defaults")
            self.bm25_stopwords = self._get_default_stopwords()
            return

        try:
            with open(stop_path, encoding="utf-8") as f:
                # 每行一个词，跳过注释（# 开头）和空行
                stopwords = set()
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    stopwords.add(line)

            self.bm25_stopwords = stopwords
            logger.info(f"Loaded BM25 stopwords: {len(stopwords)} words from {stop_path}")
        except Exception as e:
            logger.error(f"Failed to load BM25 stopwords: {e}")
            self.bm25_stopwords = self._get_default_stopwords()

    async def _load_jieba_custom_dict(self):
        """加载 jieba 自定义词典"""
        dict_file = self.config.bm25_custom_dict_file
        if not dict_file:
            logger.debug("No jieba custom dict file configured")
            return

        dict_path = Path(dict_file)
        if not dict_path.exists():
            logger.warning(f"Jieba custom dict not found: {dict_path}")
            return

        try:
            # jieba.load_userdict 是同步方法，直接调用
            jieba.load_userdict(str(dict_path))
            logger.info(f"Loaded jieba custom dict: {dict_path}")
        except Exception as e:
            logger.error(f"Failed to load jieba custom dict: {e}")

    async def _query_bm25_only(
        self,
        workspace: str,
        query: str,
    ) -> dict[str, Any]:
        """纯 BM25 检索"""
        await self._ensure_bm25_runtime()
        assert self.bm25_storage_manager is not None

        # 1. 对查询进行分词
        query_tokenized = self._jieba_tokenize(query)
        bm25_top_k = max(1, int(getattr(self.config, "bm25_top_k", 60) or 60))
        logger.info(
            "BM25 retrieval start: workspace=%s top_k=%s query=%s",
            workspace,
            bm25_top_k,
            query,
        )

        # 2. BM25 检索
        try:
            bm25_results = await (
                await self.bm25_storage_manager.get_storage(workspace)
            ).query_bm25(
                query=query_tokenized,
                workspace=workspace,
                top_k=bm25_top_k,
            )
            if not bm25_results:
                logger.info(
                    "BM25 retrieval done: workspace=%s top_k=%s query=%s "
                    "matched_chunks=0 hydrated_chunks=0",
                    workspace,
                    bm25_top_k,
                    query,
                )
                return {"status": "failure", "data": {"chunks": []}}
        except Exception as e:
            logger.error(f"BM25 retrieval failed: {e}")
            return {"status": "failure", "data": {"chunks": []}}

        # 2. 获取完整的 chunk 信息
        chunk_ids = [chunk_id for chunk_id, _ in bm25_results]
        chunk_map = {}
        try:
            chunk_map = await self.aget_chunks_by_ids(workspace, chunk_ids)
        except Exception as e:
            logger.warning(f"get chunks by ids failed: '{e}'")

        # 3. 构建结果
        chunks = []
        for chunk_id, bm25_score in bm25_results:
            chunk_data = chunk_map.get(chunk_id)
            if chunk_data:
                chunk_data["bm25_score"] = bm25_score
                chunks.append(chunk_data)

        # 5. 构建 bm25_metadata
        bm25_chunk_ids = [cid for cid, _ in bm25_results]
        bm25_chunks_with_ids = [
            f"{c.get('chunk_id')}|{c.get('content', '')}" for c in chunks if c.get("chunk_id")
        ]
        logger.info(
            "BM25 retrieval done: workspace=%s top_k=%s query=%s "
            "matched_chunks=%s hydrated_chunks=%s",
            workspace,
            bm25_top_k,
            query,
            len(bm25_results),
            len(chunks),
        )

        return {
            "status": "success",
            "data": {"chunks": chunks},
            "langfuse_trace": {
                "bm25_chunk_ids": bm25_chunk_ids,
                "bm25_chunks_with_ids": bm25_chunks_with_ids,
            },
        }

    async def _query_hybrid(
        self,
        workspace: str,
        query: str,
        param: QueryParam,
        bm25_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """混合检索：KG mix 结果 + BM25 去重后追加。"""
        await self._ensure_bm25_runtime()
        assert self.bm25_storage_manager is not None

        lexical_query = query if bm25_keywords is None else self._bm25_keyword_text(bm25_keywords)
        query_tokenized = self._jieba_tokenize(lexical_query)
        vector_top_k = max(
            1,
            int(
                (
                    getattr(param, "chunk_top_k", None)
                    if param is not None
                    else getattr(self.config, "vector_retrieval_top_k", None)
                )
                or getattr(self.config, "kg_vector_chunk_top_k", 8)
                or 8
            ),
        )
        bm25_top_k = max(1, int(getattr(self.config, "bm25_top_k", 60) or 60))

        response_result: dict[str, Any] = {
            "status": "failure",
            "data": {
                "chunks": [],
                "entities": [],
                "relationships": [],
                "references": [],
            },
            "langfuse_trace": {},
        }
        vector_chunks: list[dict[str, Any]] = []
        pre_topk_chunk_ids: list[str] = []
        output_data: dict[str, Any] = {}
        try:
            response_result = await self._query_mix_compatible(
                workspace,
                query,
                chunk_top_k=getattr(param, "chunk_top_k", None) if param is not None else None,
            )
            vector_chunks = list(response_result.get("data", {}).get("chunks", []) or [])
            langfuse_trace = response_result.get("langfuse_trace", {})
            pre_topk_chunk_ids = list(langfuse_trace.get("pre_topk_chunk_ids", []) or [])
            output_data = dict(langfuse_trace.get("output_data", {}) or {})
        except Exception:
            logger.exception(
                "Hybrid retrieval vector path failed, fallback to BM25-only: "
                "workspace=%s top_k=%s query=%s",
                workspace,
                vector_top_k,
                query,
            )

        bm25_results: list[tuple[str, float]] = []
        logger.info(
            "Hybrid BM25 retrieval start: workspace=%s top_k=%s query=%s",
            workspace,
            bm25_top_k,
            query,
        )
        try:
            bm25_results = await (
                await self.bm25_storage_manager.get_storage(workspace)
            ).query_bm25(
                query_tokenized,
                workspace,
                bm25_top_k,
            )
        except Exception as e:
            logger.warning(f"BM25 retrieval failed: {e}")
        if not vector_chunks and not bm25_results:
            return {
                "status": "failure",
                "data": {
                    "chunks": [],
                    "entities": [],
                    "relationships": [],
                    "references": [],
                },
                "langfuse_trace": {
                    "pre_topk_chunk_ids": pre_topk_chunk_ids,
                    "output_data": output_data,
                    "chunk_ids": [],
                    "chunks_with_ids": [],
                    "bm25_chunk_ids": [],
                    "bm25_chunks_with_ids": [],
                    "combine_chunks_with_ids": [],
                },
            }
        # 3. 获取 BM25 命中的完整 chunk，随后按两个结果集的名次做 RRF 融合。
        bm25_ids_to_fetch = [cid for cid, _ in bm25_results]
        if bm25_ids_to_fetch:
            bm25_full_map = await self.aget_chunks_by_ids(workspace, bm25_ids_to_fetch)
            bm25_chunks = [bm25_full_map[cid] for cid in bm25_ids_to_fetch if cid in bm25_full_map]
        else:
            bm25_chunks = []
        logger.info(
            "Hybrid retrieval done: workspace=%s query=%s vector_top_k=%s bm25_top_k=%s "
            "vector_chunks=%s bm25_matched=%s bm25_added=%s combined_chunks=%s",
            workspace,
            query,
            vector_top_k,
            bm25_top_k,
            len(vector_chunks),
            len(bm25_results),
            len(bm25_chunks),
            len(vector_chunks) + len(bm25_chunks),
        )
        rrf_k = max(1, int(getattr(self.config, "bm25_rrf_k", 60) or 60))
        combined_chunks = self._fuse_ranked_chunks(vector_chunks, bm25_chunks, rrf_k=rrf_k)
        # 6. 构建 langfuse_trace 字段
        chunk_ids = [c.get("chunk_id") for c in vector_chunks if c.get("chunk_id")]
        chunks_with_ids = [
            f"{c.get('chunk_id')}|{c.get('content', '')}"
            for c in vector_chunks
            if c.get("chunk_id")
        ]
        bm25_chunk_ids = [cid for cid, _ in bm25_results]
        bm25_chunks_with_ids = [
            f"{c.get('chunk_id')}|{c.get('content', '')}" for c in bm25_chunks if c.get("chunk_id")
        ]
        combine_chunks_with_ids = [
            f"{c.get('chunk_id')}|{c.get('content', '')}"
            for c in combined_chunks
            if c.get("chunk_id")
        ]
        # 7. 返回
        original_data = response_result.get("data", {})
        return {
            "status": response_result.get("status", "success"),
            "data": {
                "chunks": combined_chunks,
                "entities": original_data.get("entities", []),
                "relationships": original_data.get("relationships", []),
                "references": original_data.get("references", []),
            },
            "langfuse_trace": {
                "pre_topk_chunk_ids": pre_topk_chunk_ids,
                "output_data": output_data,
                "chunk_ids": chunk_ids,
                "chunks_with_ids": chunks_with_ids,
                "bm25_chunk_ids": bm25_chunk_ids,
                "bm25_chunks_with_ids": bm25_chunks_with_ids,
                "combine_chunks_with_ids": combine_chunks_with_ids,
                "rrf_k": rrf_k,
            },
        }

    @staticmethod
    def _bm25_keyword_text(keywords: list[str]) -> str:
        if (
            not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(keyword, str) or not keyword.strip() for keyword in keywords)
        ):
            raise ValueError("bm25_keywords must be a non-empty list of non-empty strings")
        return " ".join(keywords)

    @staticmethod
    def _fuse_ranked_chunks(
        vector_chunks: list[dict[str, Any]],
        bm25_chunks: list[dict[str, Any]],
        *,
        rrf_k: int,
    ) -> list[dict[str, Any]]:
        ranked: dict[tuple[str, str], tuple[float, int, dict[str, Any]]] = {}
        for source_index, source in enumerate((vector_chunks, bm25_chunks)):
            seen: set[tuple[str, str]] = set()
            for rank, chunk in enumerate(source, start=1):
                key = (str(chunk.get("workspace") or ""), str(chunk.get("chunk_id") or ""))
                if not key[1] or key in seen:
                    continue
                seen.add(key)
                score, vector_rank, existing = ranked.get(key, (0.0, 10**9, chunk))
                ranked[key] = (
                    score + 1.0 / (rrf_k + rank),
                    rank if source_index == 0 else vector_rank,
                    existing,
                )
        return [
            chunk for _, _, chunk in sorted(ranked.values(), key=lambda item: (-item[0], item[1]))
        ]

    async def aquery_data_cross_workspace(
        self,
        *,
        query: str,
        bm25_keywords: list[str],
        library_ids: Collection[str] | None = None,
        chunk_top_k: int | None = None,
        rerank_score_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Hybrid search over one authorized range of ordinary library indexes.

        None selects all ordinary catalog libraries; [] grants access to none.
        Semantic embedding/reranking and original BM25 keywords are independent.
        Unsupported backends and retrieval failures are raised, never downgraded.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        lexical_query = self._bm25_keyword_text(bm25_keywords)
        if library_ids is not None and (
            not isinstance(library_ids, Collection)
            or isinstance(library_ids, (str, bytes, Mapping))
            or any(not isinstance(item, str) or not item.strip() for item in library_ids)
        ):
            raise ValueError("library_ids must be a collection of non-empty strings or None")
        if chunk_top_k is not None and (type(chunk_top_k) is not int or chunk_top_k <= 0):
            raise ValueError("chunk_top_k must be a positive integer")
        if rerank_score_threshold is not None and (
            isinstance(rerank_score_threshold, bool)
            or not isinstance(rerank_score_threshold, (float, int))
            or not math.isfinite(rerank_score_threshold)
            or not 0 <= rerank_score_threshold <= 1
        ):
            raise ValueError("rerank_score_threshold must be finite and between 0 and 1")
        if getattr(self.config, "bm25_storage_backend", "postgres") != "postgres":
            raise NotImplementedError("Cross-workspace retrieval requires PostgreSQL BM25")

        def no_libraries() -> dict[str, Any]:
            return {
                "status": "failure",
                "reason": "no_accessible_libraries",
                "data": {"chunks": [], "entities": [], "relationships": [], "references": []},
            }

        if library_ids is not None and not library_ids:
            return no_libraries()
        catalog = await self._get_knowledge_discovery_catalog_storage()
        await self._wait_for_knowledge_discovery_catalog()
        libraries = await catalog.list_libraries()
        internal_ids = {
            PRECOMPILED_LIBRARY_ID,
            str(getattr(self.config, "precompile_workspace", PRECOMPILED_WORKSPACE)),
        }
        allowed = set(library_ids) if library_ids is not None else None
        workspaces = list(
            dict.fromkeys(
                library.library_id
                for library in libraries
                if library.library_id not in internal_ids
                and (allowed is None or library.library_id in allowed)
            )
        )
        if not workspaces:
            return no_libraries()

        top_chunks = chunk_top_k or int(
            getattr(self.config, "vector_retrieval_top_k", None)
            or self.config.kg_vector_chunk_top_k
            or 8
        )
        experiment = await self._get_kg_experiment(workspaces[0])
        await self._ensure_bm25_runtime()
        query_tokenized = self._jieba_tokenize(lexical_query, strict=True)
        if not query_tokenized.strip():
            raise ValueError("bm25_keywords must contain searchable tokens")
        raw, bm25_rows = await asyncio.gather(
            experiment.query_cross_workspace(
                query=query,
                workspaces=workspaces,
                top_entities=max(1, int(self.config.kg_vector_entity_top_k or 10)),
                top_relations=max(1, int(self.config.kg_vector_relation_top_k or 12)),
                top_chunks=top_chunks,
            ),
            self.bm25_storage_manager.query_cross_workspace(
                query_tokenized,
                workspaces=workspaces,
                top_k=max(1, int(getattr(self.config, "bm25_top_k", 60) or 60)),
            ),
        )
        for item in raw["entities"] + raw["relations"] + raw["chunks"] + bm25_rows:
            if not item.get("workspace") or item["workspace"] not in workspaces:
                raise RuntimeError("Retrieval returned an unauthorized workspace")
        bm25_keys = [(row["workspace"], row["id"]) for row in bm25_rows]
        stored_rows = await experiment.get_cross_workspace_chunks(chunk_keys=bm25_keys)
        stored_map = {(row["workspace"], row["item_id"]): row for row in stored_rows}
        if set(bm25_keys) - stored_map.keys():
            raise RuntimeError("BM25 source chunks are missing from the vector store")
        bm25_hits = []
        for row in bm25_rows:
            stored = stored_map[(row["workspace"], row["id"])]
            bm25_hits.append(
                {
                    "workspace": stored["workspace"],
                    "chunk_id": stored["item_id"],
                    "file_path": stored.get("file_path", ""),
                    "text": stored.get("content", ""),
                    "metadata": stored.get("metadata", {}),
                    "bm25_score": row["bm25_score"],
                }
            )

        async def hydrate(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for hit in hits:
                workspace = hit["workspace"]
                if workspace not in workspaces:
                    raise RuntimeError("Retrieval returned an unauthorized workspace")
                grouped.setdefault(workspace, []).append(hit)
            by_key = {}
            for workspace, group in grouped.items():
                source_map = {hit["chunk_id"]: hit for hit in group}
                for chunk in await self._hydrate_mix_chunks(workspace, group):
                    source = source_map[chunk["chunk_id"]]
                    chunk["workspace"] = workspace
                    chunk["metadata"] = {
                        **(source.get("metadata") or {}),
                        **(chunk.get("metadata") or {}),
                    }
                    for field in ("bm25_score", "entity_hits", "relation_hits", "matched_terms"):
                        if field in source:
                            chunk[field] = source[field]
                    by_key[(workspace, chunk["chunk_id"])] = chunk
            return [by_key[(hit["workspace"], hit["chunk_id"])] for hit in hits]

        vector_chunks, bm25_chunks = await asyncio.gather(
            hydrate(raw["chunks"]), hydrate(bm25_hits)
        )
        chunks = self._fuse_ranked_chunks(
            vector_chunks,
            bm25_chunks,
            rrf_k=max(1, int(getattr(self.config, "bm25_rrf_k", 60) or 60)),
        )
        rerank_top_k = max(1, int(getattr(self.config, "rerank_top_k", 50) or 50))
        chunks = chunks[:rerank_top_k]
        if getattr(self.config, "rerank_enabled", True):
            chunks = await self._rerank_query_chunks(
                workspace=workspaces[0],
                query=query,
                chunks=chunks,
                rerank_top_k=rerank_top_k,
                score_threshold=rerank_score_threshold,
                strict=True,
            )
        chunks = chunks[:top_chunks]
        references = []
        reference_by_key: dict[tuple[str, str], str] = {}
        for chunk in chunks:
            key = (chunk["workspace"], str(chunk.get("file_path") or ""))
            if key not in reference_by_key:
                reference_id = str(len(references) + 1)
                reference_by_key[key] = reference_id
                references.append(
                    {
                        "reference_id": reference_id,
                        "workspace": key[0],
                        "file_path": key[1],
                        "chunk_ids": [],
                    }
                )
            chunk["reference_id"] = reference_by_key[key]
            references[int(chunk["reference_id"]) - 1]["chunk_ids"].append(chunk["chunk_id"])
        entities = [
            {
                **item,
                "description": item["entity_description"],
                "source_id": self._graph_source_id(item["chunk_ids"]),
                "weight": item["vector_score"],
            }
            for item in raw["entities"]
        ]
        relationships = [
            {
                **item,
                "src_id": item["source_entity"],
                "tgt_id": item["target_entity"],
                "description": item["relationship_description"],
                "source_id": self._graph_source_id(item["chunk_ids"]),
                "weight": item["vector_score"],
                "keywords": item["relation_keywords"],
            }
            for item in raw["relations"]
        ]
        return {
            "status": "success",
            "data": {
                "chunks": chunks,
                "entities": entities,
                "relationships": relationships,
                "references": references,
            },
        }

    async def aquery_data(
        self,
        workspace: str,
        query: str,
        mode: str = "hybrid",
        where: dict[str, Any] | None = None,
        retrieval_mode: str = "hybrid",  # 检索模式：bm25/vector/hybrid
        chunk_top_k: int | None = None,
        max_total_tokens: int | None = None,
        rerank_enabled: bool | None = None,
        rerank_score_threshold: float | None = None,
        bm25_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        查询接口（返回结构化数据，支持 metadata 过滤）

        Args:
            workspace: 工作空间名称
            query: 查询问题
            mode: KG 向量检索模式
                影响实体/关系检索策略。
                "local"/"global"/"hybrid"/"naive"/"mix"/"bypass"
            retrieval_mode: 检索模式
                "bm25" - 纯 BM25 全文检索
                "vector" - 纯向量检索
                "hybrid" - 混合检索（BM25 + 向量 + RRF 融合）
            where: 可选的过滤条件（metadata 条件表达式）
            chunk_top_k: 可选的文本块检索数量
            max_total_tokens: 可选的最大上下文 token 数
            rerank_enabled: 可选的请求级 rerank 开关；None 时使用 config.rerank_enabled
            rerank_score_threshold: 可选的请求级 rerank 分数阈值；
                None 时使用 config.rerank_score_threshold
            bm25_keywords: 可选的原始词法关键词，独立分词，不影响语义 query

        Returns:
            查询结果
        """
        lexical_query = query if bm25_keywords is None else self._bm25_keyword_text(bm25_keywords)
        query_param_dict = {"mode": mode}
        if chunk_top_k is not None:
            query_param_dict["chunk_top_k"] = int(chunk_top_k)
        if max_total_tokens is not None:
            query_param_dict["max_total_tokens"] = int(max_total_tokens)
        query_param = QueryParam(**query_param_dict)

        langfuse = get_client()
        # 初始化 langfuse trace自定义信息
        pre_topk_chunk_ids: list[str] = []
        output_data: dict[str, list[str]] = {
            "vector_chunks": [],
            "entity_chunks": [],
            "relation_chunks": [],
            "merged_chunks": [],
        }
        chunks_with_ids = []
        chunk_ids = []
        bm25_chunk_ids = []
        bm25_chunks_with_ids = []
        combine_chunks_with_ids = []
        base_entities: list[dict[str, Any]] = []
        base_relationships: list[dict[str, Any]] = []
        try:
            if retrieval_mode == "bm25":
                # 纯 BM25 检索
                result = await self._query_bm25_only(workspace, lexical_query)
                base_chunks = list(result.get("data", {}).get("chunks", []) or [])
                base_entities = list(result.get("data", {}).get("entities", []) or [])
                base_relationships = list(result.get("data", {}).get("relationships", []) or [])

                # 从 result["langfuse_trace"] 中获取构建metadata的内容
                langfuse_trace = result.get("langfuse_trace", {})
                bm25_chunk_ids = langfuse_trace.get("bm25_chunk_ids", [])
                bm25_chunks_with_ids = langfuse_trace.get("bm25_chunks_with_ids", [])

            elif retrieval_mode == "hybrid":
                if bm25_keywords is None:
                    result = await self._query_hybrid(workspace, query, query_param)
                else:
                    result = await self._query_hybrid(
                        workspace, query, query_param, bm25_keywords=bm25_keywords
                    )
                base_chunks = list(result.get("data", {}).get("chunks", []) or [])
                base_entities = list(result.get("data", {}).get("entities", []) or [])
                base_relationships = list(result.get("data", {}).get("relationships", []) or [])

                # 从 result["langfuse_trace"] 中获取构建metadata的内容
                langfuse_trace = result.get("langfuse_trace", {})
                # KG 检索相关
                pre_topk_chunk_ids = langfuse_trace.get("pre_topk_chunk_ids", [])
                output_data = langfuse_trace.get("output_data", {})
                chunk_ids = langfuse_trace.get("chunk_ids", [])
                chunks_with_ids = langfuse_trace.get("chunks_with_ids", [])
                # BM25 相关
                bm25_chunk_ids = langfuse_trace.get("bm25_chunk_ids", [])
                bm25_chunks_with_ids = langfuse_trace.get("bm25_chunks_with_ids", [])
                # BM25 与 Light 融合后结果
                combine_chunks_with_ids = langfuse_trace.get("combine_chunks_with_ids", [])
            elif retrieval_mode == "vector":
                # 单模式 mix 查询（保留 retrieval_mode 接口兼容）
                result = await self._query_mix_compatible(
                    workspace,
                    query,
                    chunk_top_k=chunk_top_k,
                )

                base_chunks = list(result.get("data", {}).get("chunks", []) or [])
                base_entities = list(result.get("data", {}).get("entities", []) or [])
                base_relationships = list(result.get("data", {}).get("relationships", []) or [])
                langfuse_trace = result.get("langfuse_trace", {})
                pre_topk_chunk_ids = langfuse_trace.get("pre_topk_chunk_ids", [])
                output_data = langfuse_trace.get("output_data", {})
                chunk_ids = langfuse_trace.get("chunk_ids", [])
                chunks_with_ids = langfuse_trace.get("chunks_with_ids", [])
        except Exception as e:
            base_chunks = []
            base_entities = []
            base_relationships = []
            result = {
                "status": "failure",  # 或 "failure"，根据业务逻辑决定
                "data": {"chunks": [], "entities": [], "relationships": [], "references": []},
            }
            logger.exception(f"{retrieval_mode} retrieval failed: {e}")

        detail_chunks = base_chunks.copy()
        entity_top_k = max(1, int(self.config.kg_vector_entity_top_k or 10))
        relation_top_k = max(1, int(self.config.kg_vector_relation_top_k or 12))
        delta_graph = await self._query_vkr_delta_graph(
            workspace=workspace,
            query=query,
            top_k=int(self.config.vkr_delta_top_k),
            entity_top_k=entity_top_k,
        )

        union_entities, entity_pruned = self._merge_entities(
            base_entities,
            list(delta_graph.get("entities") or []),
            top_k=entity_top_k,
        )
        union_relationships, relation_pruned = self._merge_relationships(
            base_relationships,
            list(delta_graph.get("relationships") or []),
            top_k=relation_top_k,
        )

        union_chunk_ids = self._collect_source_chunk_ids(union_entities, union_relationships)
        direct_chunk_ids = [
            str(chunk.get("chunk_id") or "")
            for chunk in detail_chunks
            if str(chunk.get("chunk_id") or "").strip()
        ]
        existing_chunk_id_set = set(direct_chunk_ids)
        union_expand_ids = [
            chunk_id for chunk_id in union_chunk_ids if chunk_id not in existing_chunk_id_set
        ]

        union_expanded_chunks: list[dict[str, Any]] = []
        if union_expand_ids:
            expanded_map = await self.aget_chunks_by_ids(workspace, union_expand_ids)
            union_expanded_chunks = [
                expanded_map[chunk_id] for chunk_id in union_expand_ids if chunk_id in expanded_map
            ]
            if union_expanded_chunks:
                detail_chunks = self._union_vkr_chunks(detail_chunks, union_expanded_chunks)

        if not union_expanded_chunks:
            delta_chunks = list(delta_graph.get("chunks") or [])
            if delta_chunks:
                detail_chunks = self._union_vkr_chunks(detail_chunks, delta_chunks)

        detail_chunks = self._dedupe_chunks_by_canonical_id(detail_chunks)
        pre_rerank_chunk_ids = [
            str(chunk.get("chunk_id") or "")
            for chunk in detail_chunks
            if str(chunk.get("chunk_id") or "").strip()
        ]

        should_rerank = (
            bool(getattr(self.config, "rerank_enabled", True))
            if rerank_enabled is None
            else bool(rerank_enabled)
        )
        rerank_top_k = max(1, int(getattr(self.config, "rerank_top_k", 50) or 50))
        detail_chunks = detail_chunks[:rerank_top_k]
        if should_rerank:
            rerank_kwargs: dict[str, Any] = {
                "workspace": workspace,
                "query": query,
                "chunks": detail_chunks,
                "rerank_top_k": rerank_top_k,
            }
            if rerank_score_threshold is not None:
                rerank_kwargs["score_threshold"] = rerank_score_threshold
            detail_chunks = await self._rerank_query_chunks(
                **rerank_kwargs,
            )

        result_data = result.get("data", {})
        result_data["chunks"] = detail_chunks
        result_data["entities"] = union_entities
        result_data["relationships"] = union_relationships
        result_data["references"] = self._build_reference_list(detail_chunks)
        result["data"] = result_data
        with langfuse.start_as_current_observation(
            as_type="span", name="kg_retrieval_span"
        ) as root_span:
            root_span.update(
                input={"query": query, "retrieval_mode": retrieval_mode},
                output={
                    "direct_vector_chunk_ids": chunk_ids,
                    "vector_merge_pre_topk_chunk_ids": pre_topk_chunk_ids,
                    "direct_vector_chunks_with_ids": chunks_with_ids,
                    "vector_entity_hit_chunks": output_data.get("entity_chunks", []),
                    "vector_relation_hit_chunks": output_data.get("relation_chunks", []),
                    "bm25_chunk_ids": bm25_chunk_ids,
                    "bm25_chunks_with_ids": bm25_chunks_with_ids,
                    "hybrid_merged_chunk_ids": combine_chunks_with_ids,
                    "federated_union_chunk_ids": [
                        str(chunk.get("chunk_id") or "")
                        for chunk in union_expanded_chunks
                        if str(chunk.get("chunk_id") or "").strip()
                    ],
                    "federated_union_entity_ids": [
                        str(entity.get("entity_name") or "")
                        for entity in union_entities
                        if str(entity.get("entity_name") or "").strip()
                    ],
                    "federated_union_relation_ids": [
                        f"{str(relation.get('src_id') or '')}|{str(relation.get('tgt_id') or '')}|"
                        f"{str(relation.get('description') or '')}"
                        for relation in union_relationships
                    ],
                    "final_chunk_ids_before_rerank": pre_rerank_chunk_ids,
                    "delta_hit_count": int(
                        (delta_graph.get("stats") or {}).get("matched_relations") or 0
                    ),
                    "base_hit_count": len(chunk_ids),
                    "union_pruned_by_topk": {
                        "entities": entity_pruned,
                        "relationships": relation_pruned,
                    },
                },
            )
        # 统一 where 过滤
        # 无 where 时单独注入 title
        _chunks_for_title = result.get("data", {}).get("chunks", [])
        if _chunks_for_title:
            _doc_ids = list(
                {c.get("full_doc_id", "") for c in _chunks_for_title if c.get("full_doc_id")}
            )
            _status_map: dict[str, Any] = {}
            if _doc_ids:
                try:
                    _st = get_asset_status_storage()
                    _status_map = await _st.get_status_list(workspace, _doc_ids)
                except Exception as _e:
                    logger.debug(f"aquery_data title inject: get_status_list failed: {_e}")
            HyperRAGV2._inject_title_into_chunks(_chunks_for_title, _status_map)
        if not where:
            return result
        return await self._filter_query_data_by_where(workspace, result, where)

    def query_data(self, *args, **kwargs) -> dict[str, Any]:
        """查询接口（同步包装）"""
        return asyncio.run(self.aquery_data(*args, **kwargs))

    async def aquery_data_by_image(
        self,
        *,
        workspace: str,
        image_url: str,
        query_text: str = "",
        mix_search: bool = True,
        chunk_top_k: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve chunks by single-image (or single image+text) embedding search."""
        normalized_image_url = self._require_base64_image_data_url(image_url)
        await self._ensure_core_runtime()
        normalized_query_text = str(query_text or "").strip()
        should_mix = bool(mix_search and normalized_query_text)
        default_image_top_k = getattr(self.config, "image_retrieval_top_k", 3)
        top_k = max(1, int(default_image_top_k or 3))

        embedder = self.multimodal_embedder
        if embedder is None:
            raise ValueError("multimodal embedder is required for image retrieval")

        if should_mix:
            image_query_vector = await embedder.embed_image_text(
                normalized_image_url,
                normalized_query_text,
                instruction=(
                    "Encode this image and text query for retrieving the most semantically "
                    "relevant multimodal knowledge chunks."
                ),
            )
        else:
            image_query_vector = await embedder.embed_image(
                normalized_image_url,
                instruction=(
                    "Encode this image for retrieving the most semantically relevant "
                    "multimodal knowledge chunks."
                ),
            )
        if not isinstance(image_query_vector, list) or not image_query_vector:
            raise RuntimeError("multimodal embedder returned an invalid image query embedding")

        experiment = await self._get_kg_experiment(workspace)
        rows = await experiment._get_vector_store().query_chunks(
            query_vector=[float(item) for item in image_query_vector],
            workspace=workspace,
            top_k=top_k,
            min_similarity=-1.0,
        )
        status_map = await self._get_workspace_asset_status_cache(workspace)
        status_by_file_path = self._build_status_by_file_path(status_map)
        detail_chunks = [
            await self._normalize_kg_chunk_row(
                workspace,
                row,
                status_map=status_map,
                status_by_file_path=status_by_file_path,
            )
            for row in rows
        ]

        detail_chunks = self._dedupe_chunks_by_canonical_id(detail_chunks)

        retrieval_mode = "image_text_mixed" if should_mix else "image_only"
        return {
            "status": "success",
            "data": {
                "chunks": detail_chunks,
                "entities": [],
                "relationships": [],
                "references": self._build_reference_list(detail_chunks),
            },
            "metadata": {
                "retrieval_mode": retrieval_mode,
                "result_count": len(detail_chunks),
                "mix_search": bool(should_mix),
                "query_text": normalized_query_text,
            },
        }

    def query_data_by_image(self, *args, **kwargs) -> dict[str, Any]:
        """Single-image retrieval sync wrapper."""
        return asyncio.run(self.aquery_data_by_image(*args, **kwargs))

    def _build_rerank_api_url(self) -> str:
        base_url = str(getattr(self.config, "rerank_base_url", "") or "").strip()
        if not base_url:
            base_url = "http://ai.qingxiang.tech:8602/v1"
        base_url = base_url.rstrip("/")
        if base_url.endswith("/rerank"):
            return base_url
        return f"{base_url}/rerank"

    @staticmethod
    def _chunk_rerank_text(chunk: dict[str, Any]) -> str:
        parts: list[str] = []
        metadata = chunk.get("metadata")
        if isinstance(metadata, dict):
            title = metadata.get("title") or metadata.get("asset_title")
            if title:
                HyperRAGV2._append_rerank_tag(parts, "title", title)
            embedding_text = metadata.get("embedding_text")
            if embedding_text:
                parts.append(HyperRAGV2._clean_rerank_text(embedding_text))
        for key in ("content", "context", "text"):
            value = chunk.get(key)
            if value:
                HyperRAGV2._append_rerank_tag(parts, key, value)
        text = "\n".join(part for part in parts if part)
        return text or "None"

    @staticmethod
    def _clean_rerank_text(value: Any) -> str:
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, (list, tuple, set)):
            return "；".join(
                part for part in (HyperRAGV2._clean_rerank_text(item) for item in value) if part
            )
        if isinstance(value, dict):
            return "；".join(
                part
                for part in (HyperRAGV2._clean_rerank_text(item) for item in value.values())
                if part
            )
        return " ".join(str(value).split())

    @staticmethod
    def _append_rerank_tag(parts: list[str], tag: str, value: Any) -> None:
        text = HyperRAGV2._clean_rerank_text(value)
        if not text:
            return
        parts.append(f"<{tag}>{text}</{tag}>")

    @staticmethod
    def _metadata_float(metadata: dict[str, Any], key: str) -> float | None:
        value = metadata.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_rerank_video_clip(
        *,
        source_path: Path,
        begin_ts: float,
        end_ts: float,
        output_dir: Path,
    ) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", source_path.stem).strip("_")
        if not safe_name:
            safe_name = "video"
        output_path = output_dir / f"{safe_name}_{begin_ts:.3f}_{end_ts:.3f}.mp4"
        duration = max(0.1, end_ts - begin_ts)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{begin_ts:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-an",
            str(output_path),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"failed to create rerank video clip: {output_path}")
        return output_path

    @staticmethod
    def _chunk_rerank_media(
        chunk: dict[str, Any],
        *,
        clip_dir: Path | None = None,
    ) -> list[Any]:
        from .rerank import RerankMedia

        metadata = chunk.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        retrieved_by = str(metadata.get("retrieved_by") or "")
        chunk_type = str(metadata.get("chunk_type") or "")
        if "video" not in retrieved_by and chunk_type != "video":
            return []

        source_url = str(metadata.get("source_url") or "").strip()
        if source_url.startswith(("http://", "https://", "data:")):
            return [RerankMedia(kind="video", data_url=source_url)]

        file_path = str(chunk.get("file_path") or metadata.get("file_path") or "").strip()
        if file_path:
            source_path = Path(file_path)
            if source_path.is_file():
                begin_ts = HyperRAGV2._metadata_float(metadata, "begin_ts")
                end_ts = HyperRAGV2._metadata_float(metadata, "end_ts")
                if begin_ts is not None and end_ts is not None and end_ts > begin_ts:
                    if clip_dir is not None:
                        try:
                            clip_path = HyperRAGV2._extract_rerank_video_clip(
                                source_path=source_path,
                                begin_ts=max(0.0, begin_ts),
                                end_ts=end_ts,
                                output_dir=clip_dir,
                            )
                            return [RerankMedia(kind="video", path=clip_path)]
                        except Exception:
                            logger.exception(
                                f"Failed to create rerank video clip from {source_path}; "
                                "falling back to source video"
                            )
                return [RerankMedia(kind="video", path=source_path)]
        return []

    async def _rerank_query_chunks(
        self,
        workspace: str,
        query: str,
        chunks: list[dict[str, Any]],
        *,
        rerank_top_k: int | None = None,
        score_threshold: float | None = None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        del workspace
        if not chunks:
            return chunks

        from .rerank import OpenAICompatibleReranker, RerankCandidate

        effective_rerank_top_k = rerank_top_k
        if effective_rerank_top_k is None:
            effective_rerank_top_k = getattr(self.config, "rerank_top_k", 50)
        effective_rerank_top_k = max(1, int(effective_rerank_top_k or 50))
        candidate_chunks = list(chunks[:effective_rerank_top_k])
        clip_temp_dir = (
            None if strict else tempfile.TemporaryDirectory(prefix="hyperrag_rerank_clips_")
        )
        try:
            clip_dir = Path(clip_temp_dir.name) if clip_temp_dir is not None else Path(".")
            configured_mode = (
                "text" if strict else str(getattr(self.config, "rerank_mode", "text") or "text")
            )
            candidates = [
                RerankCandidate(
                    id=str(index),
                    text=self._chunk_rerank_text(chunk),
                    media=(
                        []
                        if configured_mode == "text"
                        else self._chunk_rerank_media(chunk, clip_dir=clip_dir)
                    ),
                    metadata={"chunk_index": index},
                )
                for index, chunk in enumerate(candidate_chunks)
            ]
            if not candidates:
                return chunks

            rerank_timeout = max(
                0.001,
                float(getattr(self.config, "rerank_timeout_seconds", 120.0) or 120.0),
            )

            async def run_rerank(rerank_candidates, *, mode: str):
                logger.info(
                    f"Query rerank started for {len(rerank_candidates)} candidates "
                    f"(mode={mode}, timeout={rerank_timeout:.3f}s)"
                )
                reranker = OpenAICompatibleReranker(
                    api_key=str(getattr(self.config, "rerank_api_key", "") or ""),
                    model=str(
                        getattr(
                            self.config,
                            "rerank_model",
                            "Qwen/Qwen3-VL-Reranker-2B",
                        )
                        or "Qwen/Qwen3-VL-Reranker-2B"
                    ),
                    api_url=self._build_rerank_api_url(),
                    mode=mode,
                    batch_size=int(getattr(self.config, "rerank_batch_size", 4) or 4),
                    batch_concurrency=int(getattr(self.config, "rerank_batch_concurrency", 4) or 4),
                    max_inline_media_mb=float(
                        getattr(self.config, "rerank_max_inline_media_mb", 48.0) or 48.0
                    ),
                    max_retries=int(getattr(self.config, "rerank_max_retries", 2) or 2),
                    timeout_seconds=rerank_timeout,
                )
                results = await asyncio.wait_for(
                    reranker.rerank(query, rerank_candidates),
                    timeout=rerank_timeout,
                )
                logger.info(f"Query rerank returned {len(results)} results (mode={mode})")
                return results

            try:
                rerank_results = await run_rerank(candidates, mode=configured_mode)
            except TimeoutError:
                if strict:
                    raise
                if configured_mode == "text":
                    raise
                logger.error(
                    f"Query rerank timed out after {rerank_timeout:.3f}s "
                    f"(mode={configured_mode}); falling back to text rerank"
                )
                text_candidates = [
                    RerankCandidate(
                        id=candidate.id,
                        text=candidate.text,
                        metadata=candidate.metadata,
                    )
                    for candidate in candidates
                ]
                try:
                    rerank_results = await run_rerank(text_candidates, mode="text")
                except TimeoutError:
                    logger.error(
                        f"Text query rerank timed out after {rerank_timeout:.3f}s; "
                        "using original chunk order"
                    )
                    return chunks
                except Exception:
                    logger.exception("Text query rerank failed; using original chunk order")
                    return chunks
            except Exception:
                if strict:
                    raise
                if configured_mode == "text":
                    raise
                logger.exception(
                    f"Query rerank failed (mode={configured_mode}); falling back to text rerank"
                )
                text_candidates = [
                    RerankCandidate(
                        id=candidate.id,
                        text=candidate.text,
                        metadata=candidate.metadata,
                    )
                    for candidate in candidates
                ]
                try:
                    rerank_results = await run_rerank(text_candidates, mode="text")
                except TimeoutError:
                    logger.error(
                        f"Text query rerank timed out after {rerank_timeout:.3f}s; "
                        "using original chunk order"
                    )
                    return chunks
                except Exception:
                    logger.exception("Text query rerank failed; using original chunk order")
                    return chunks
        except TimeoutError:
            if strict:
                raise
            logger.error(
                f"Query rerank timed out after {rerank_timeout:.3f}s; using original chunk order"
            )
            return chunks
        except Exception:
            if strict:
                raise
            logger.exception("Query rerank failed; using original chunk order")
            return chunks
        finally:
            if clip_temp_dir is not None:
                clip_temp_dir.cleanup()

        result_by_candidate_id = {str(result.candidate_id): result for result in rerank_results}
        if strict and (
            set(result_by_candidate_id) != {str(index) for index in range(len(candidate_chunks))}
            or len(result_by_candidate_id) != len(rerank_results)
            or any(not math.isfinite(float(result.relevance_score)) for result in rerank_results)
        ):
            raise RuntimeError("Reranker did not return one finite score per candidate")
        raw_score_threshold = (
            score_threshold
            if score_threshold is not None
            else getattr(self.config, "rerank_score_threshold", None)
        )
        score_threshold = 0.55 if raw_score_threshold is None else float(raw_score_threshold)
        ordered_results = sorted(
            [
                result
                for result in rerank_results
                if float(result.relevance_score) >= score_threshold
            ],
            key=lambda result: result.relevance_score,
            reverse=True,
        )
        if not rerank_results:
            logger.info("Query rerank returned empty results; using original candidate order")
            return chunks
        if not ordered_results:
            logger.info(
                "Query rerank returned no candidates above threshold "
                f"(threshold={score_threshold:.3f}); dropping all candidates"
            )
            return []

        used_indexes: set[int] = set()
        reranked_chunks: list[dict[str, Any]] = []
        for result in ordered_results:
            try:
                chunk_index = int(result.candidate_id)
            except Exception:
                continue
            if chunk_index < 0 or chunk_index >= len(candidate_chunks):
                continue
            if chunk_index in used_indexes:
                continue
            used_indexes.add(chunk_index)
            chunk = dict(candidate_chunks[chunk_index])
            metadata = dict(chunk.get("metadata") or {})
            metadata["rerank_score"] = result.relevance_score
            metadata["rerank_model"] = str(getattr(self.config, "rerank_model", ""))
            chunk["metadata"] = metadata
            reranked_chunks.append(chunk)

        for index, chunk in enumerate(candidate_chunks):
            if index in used_indexes:
                continue
            result = result_by_candidate_id.get(str(index))
            if result is None or float(result.relevance_score) < score_threshold:
                continue
            chunk_copy = dict(chunk)
            metadata = dict(chunk_copy.get("metadata") or {})
            metadata["rerank_score"] = result.relevance_score
            metadata["rerank_model"] = str(getattr(self.config, "rerank_model", ""))
            chunk_copy["metadata"] = metadata
            reranked_chunks.append(chunk_copy)

        return reranked_chunks

    def list_workspaces(self) -> list[str]:
        return asyncio.run(self.alist_workspaces())

    async def aget_workspace_status(self, workspace: str) -> dict | None:
        await self._ensure_core_runtime()
        statuses = await get_asset_status_storage().get_all_status(workspace)
        graph_summary = await (await self._get_kg_experiment(workspace)).get_workspace_summary()
        status_counts: dict[str, int] = {}
        for status in statuses.values():
            name = status.status.value
            status_counts[name] = status_counts.get(name, 0) + 1
        return {
            "workspace": workspace,
            "asset_count": len(statuses),
            "status_counts": status_counts,
            "graph": graph_summary,
        }

    def get_workspace_status(self, *args, **kwargs) -> dict | None:
        return asyncio.run(self.aget_workspace_status(*args, **kwargs))

    async def aget_job_status(self, job_id: str) -> dict | None:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        return await self.scheduler.get_job_status(job_id)

    def get_job_status(self, *args, **kwargs) -> dict | None:
        return asyncio.run(self.aget_job_status(*args, **kwargs))

    async def aget_task_status(self, task_id: str) -> dict | None:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        return await self.scheduler.get_task_status(task_id)

    def get_task_status(self, *args, **kwargs) -> dict | None:
        return asyncio.run(self.aget_task_status(*args, **kwargs))

    async def aretry_job(self, job_id: str) -> bool:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        return await self.scheduler.retry_job(job_id)

    def retry_job(self, *args, **kwargs) -> bool:
        return asyncio.run(self.aretry_job(*args, **kwargs))

    async def aswitch_mode(self, mode: str) -> bool:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        if mode == "read_write":
            return await self.scheduler.switch_to_rw()
        await self.scheduler.switch_to_ro()
        return True

    def switch_mode(self, *args, **kwargs) -> bool:
        return asyncio.run(self.aswitch_mode(*args, **kwargs))

    @property
    def current_mode(self) -> str:
        if self.scheduler:
            return self.scheduler.current_mode.value
        return "read_only"

    # -------------------- 对外：asset（返回类型与 hyperrag.py 一致） --------------------
    async def aget_asset_status(
        self, workspace: str, asset_id: str
    ) -> AssetProcessingStatus | None:
        st = get_asset_status_storage()
        status = await st.get_status(workspace, asset_id)
        if status is None:
            return None
        if status.status == AssetStatus.COMPLETED:
            metadata = await self.aget_asset_metadata(
                workspace,
                asset_id,
                include_chunk_count=True,
            )
            status.metadata = metadata or None
        return status

    def get_asset_status(self, *args, **kwargs) -> AssetProcessingStatus | None:
        return asyncio.run(self.aget_asset_status(*args, **kwargs))

    async def aget_knowledgebase_info(self, knowledgebase_id: str) -> KnowledgebaseInfo | None:
        storage = get_knowledgebase_info_storage()
        return await storage.get(knowledgebase_id)

    def get_knowledgebase_info(self, *args, **kwargs) -> KnowledgebaseInfo | None:
        return asyncio.run(self.aget_knowledgebase_info(*args, **kwargs))

    async def alist_all_knowledgebases(self) -> list[KnowledgebaseInfo]:
        """列出所有已索引的知识库及摘要信息。"""
        storage = get_knowledgebase_info_storage()
        return await storage.get_all()

    def list_all_knowledgebases(self) -> list[KnowledgebaseInfo]:
        return asyncio.run(self.alist_all_knowledgebases())

    async def aupsert_knowledgebase_info(
        self,
        knowledgebase_id: str,
        *,
        name: str | None = None,
        asset_ids: list[str] | None = None,
        summarization: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgebaseInfo:
        catalog = await self._get_knowledge_discovery_catalog_storage()
        async with catalog.library_transaction(knowledgebase_id) as transaction:
            storage = get_knowledgebase_info_storage().with_db(transaction.db)
            record = await storage.upsert(
                knowledgebase_id,
                name=name,
                asset_ids=asset_ids,
                summarization=summarization,
                metadata=metadata,
            )
            await transaction.synchronize_library(*self._discovery_catalog_snapshot(record))
            return record

    def upsert_knowledgebase_info(self, *args, **kwargs) -> KnowledgebaseInfo:
        return asyncio.run(self.aupsert_knowledgebase_info(*args, **kwargs))

    @staticmethod
    def _normalize_asset_summary_map(
        metadata: dict[str, Any] | None,
    ) -> dict[str, dict[str, str]]:
        if not isinstance(metadata, dict):
            return {}
        raw = metadata.get("asset_summaries")
        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, dict[str, str]] = {}
        for asset_id, payload in raw.items():
            key = str(asset_id or "").strip()
            if not key or not isinstance(payload, dict):
                continue
            summary = normalize_text(payload.get("summary"))
            title = normalize_text(payload.get("title"))
            type_label = normalize_text(payload.get("type_label"))
            source = normalize_text(payload.get("source"))
            status_updated_at = normalize_text(payload.get("status_updated_at"))
            if not summary and not title and not type_label:
                continue
            normalized[key] = {
                "title": title,
                "summary": summary,
                "type_label": type_label,
                "source": source,
                "status_updated_at": status_updated_at,
            }
        return normalized

    @staticmethod
    def _extract_file_attributes(file_data: Any) -> dict[str, Any]:
        if not isinstance(file_data, dict):
            return {}
        attributes = file_data.get("attributes")
        return attributes if isinstance(attributes, dict) else {}

    @classmethod
    def _extract_asset_title(cls, status: AssetProcessingStatus) -> str:
        file_data = status.file_data if isinstance(status.file_data, dict) else {}
        title = normalize_text(file_data.get("title"))
        if title:
            return title
        attributes = cls._extract_file_attributes(file_data)
        for key in ("title", "name"):
            value = normalize_text(attributes.get(key))
            if value:
                return value
        file_path = normalize_text(getattr(status, "file_path", None) or file_data.get("file_path"))
        if file_path:
            return Path(file_path).name
        return status.asset_id

    @staticmethod
    def _build_asset_type_label(status: AssetProcessingStatus) -> str:
        return "视频" if status.parse_mode in {"video", "sop_video", "plain_video"} else "文档"

    @staticmethod
    def _status_updated_marker(status: AssetProcessingStatus) -> str:
        updated_at = getattr(status, "updated_at", None)
        if isinstance(updated_at, datetime.datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=datetime.UTC)
            return f"{updated_at.astimezone(datetime.UTC).timestamp():.6f}"
        if updated_at is None:
            return ""
        try:
            return f"{float(updated_at):.6f}"
        except (TypeError, ValueError):
            return normalize_text(str(updated_at))

    @classmethod
    def _build_asset_summary_entry(
        cls,
        status: AssetProcessingStatus,
        summary: str,
        *,
        source: str,
        title_override: str | None = None,
    ) -> dict[str, str]:
        return {
            "title": normalize_text(title_override) or cls._extract_asset_title(status),
            "type_label": cls._build_asset_type_label(status),
            "summary": shorten_text(summary, 120),
            "source": normalize_text(source),
            "status_updated_at": cls._status_updated_marker(status),
        }

    @classmethod
    def _build_asset_summary_entry_without_status(
        cls,
        *,
        asset_id: str,
        title: str,
        summary: str,
        type_label: str = "文档",
        source: str,
    ) -> dict[str, str]:
        return {
            "title": normalize_text(title) or asset_id,
            "type_label": normalize_text(type_label) or "文档",
            "summary": shorten_text(summary, 120),
            "source": normalize_text(source),
            "status_updated_at": "",
        }

    @classmethod
    def _get_cached_asset_summary(
        cls,
        asset_summary_map: dict[str, dict[str, str]],
        status: AssetProcessingStatus,
    ) -> dict[str, str] | None:
        entry = asset_summary_map.get(status.asset_id)
        if not isinstance(entry, dict):
            return None
        marker = normalize_text(entry.get("status_updated_at"))
        if marker and marker != cls._status_updated_marker(status):
            return None
        summary = normalize_text(entry.get("summary"))
        if not summary:
            return None
        return {
            "asset_id": status.asset_id,
            "title": normalize_text(entry.get("title")) or cls._extract_asset_title(status),
            "type_label": normalize_text(entry.get("type_label"))
            or cls._build_asset_type_label(status),
            "summary": shorten_text(summary, 120),
        }

    async def _agenerate_knowledgebase_overview(
        self,
        *,
        workspace: str,
        asset_items: list[dict[str, str]],
    ) -> str:
        if not asset_items:
            return EMPTY_KNOWLEDGEBASE_SUMMARY
        if len(asset_items) == 1:
            return shorten_text(asset_items[0].get("summary") or "", 120)

        try:
            materials = "\n".join(
                f"- {item['type_label']}《{item['title']}》：{shorten_text(item['summary'], 80)}"
                for item in asset_items[:12]
            )
            prompt = (
                "你是知识库导览助手。请根据下面的已索引文件摘要，生成一段用于向用户介绍知识库内容的整体概括。\n"
                "要求：\n"
                "1) 只输出一句中文，不超过120字。\n"
                "2) 概括主题、场景和可提问方向。\n"
                "3) 不要列点，不要重复堆砌文件名。\n\n"
                f"材料：\n{materials}\n"
            )
            logger.info(
                "HyperRAGV2: generating knowledgebase overview for workspace=%s assets=%s",
                workspace,
                len(asset_items),
            )
            experiment = await self._get_kg_experiment(workspace)
            overview = await experiment._get_extractor().complete_text(
                prompt,
                system_prompt="You generate concise Chinese introductions for indexed knowledge bases.",
                max_tokens=max(96, int(self.config.summary_max_tokens or 500)),
            )
            normalized = normalize_text(overview)
            if normalized:
                return shorten_text(normalized, 120)
        except Exception as exc:
            logger.warning(
                "HyperRAGV2: failed to generate knowledgebase overview for workspace=%s: %s",
                workspace,
                exc,
            )

        return fallback_overview_from_assets(asset_items)

    async def async_knowledgebase_summary(
        self,
        knowledgebase_id: str,
        *,
        asset_id: str,
        asset_title: str | None = None,
        document_summary: str | None = None,
        remove_asset: bool = False,
    ) -> KnowledgebaseInfo:
        existing = await self.aget_knowledgebase_info(knowledgebase_id)
        status_map = await self.alist_assets(knowledgebase_id)
        if remove_asset and existing is None:
            return KnowledgebaseInfo(
                id=knowledgebase_id,
                name=knowledgebase_id,
                created_at=time.time(),
                updated_at=time.time(),
                asset_ids=[],
                summarization=EMPTY_KNOWLEDGEBASE_SUMMARY,
                metadata={"asset_summaries": {}, "asset_count": 0},
            )

        existing_metadata = (
            dict(existing.metadata or {})
            if existing and isinstance(existing.metadata, dict)
            else {}
        )
        asset_summary_map = self._normalize_asset_summary_map(existing_metadata)
        tracked_asset_ids = (
            [str(item).strip() for item in (existing.asset_ids or [])] if existing else []
        )
        if not tracked_asset_ids:
            tracked_asset_ids = [
                asset_key
                for asset_key, status in status_map.items()
                if status.status != AssetStatus.DELETED
            ]

        asset_id = str(asset_id or "").strip()
        if remove_asset:
            tracked_asset_ids = [item for item in tracked_asset_ids if item != asset_id]
            asset_summary_map.pop(asset_id, None)
        elif asset_id and asset_id not in tracked_asset_ids:
            tracked_asset_ids.append(asset_id)

        asset_items: list[dict[str, str]] = []
        refreshed_asset_summaries: dict[str, dict[str, str]] = {}
        active_asset_ids: list[str] = []
        seen: set[str] = set()

        for candidate_id in tracked_asset_ids + list(status_map.keys()):
            candidate_id = str(candidate_id or "").strip()
            if not candidate_id or candidate_id in seen:
                continue
            if remove_asset and candidate_id == asset_id:
                continue
            seen.add(candidate_id)
            status = status_map.get(candidate_id)
            if status is not None and status.status in {AssetStatus.DELETED, AssetStatus.DELETING}:
                continue
            active_asset_ids.append(candidate_id)

            if candidate_id == asset_id and not remove_asset:
                summary_text = normalize_text(document_summary)
                title_text = normalize_text(asset_title)
                if status is not None:
                    title_text = title_text or self._extract_asset_title(status)
                    if summary_text:
                        entry = self._build_asset_summary_entry(
                            status,
                            summary_text,
                            source="document_summary",
                            title_override=title_text,
                        )
                    else:
                        fallback_summary = f"与{title_text or self._extract_asset_title(status)}相关的内容已完成索引，可用于知识库检索与问答。"
                        entry = self._build_asset_summary_entry(
                            status,
                            fallback_summary,
                            source="default",
                            title_override=title_text,
                        )
                else:
                    title_text = title_text or candidate_id
                    fallback_summary = (
                        summary_text
                        or f"与{title_text}相关的内容已完成索引，可用于知识库检索与问答。"
                    )
                    entry = self._build_asset_summary_entry_without_status(
                        asset_id=candidate_id,
                        title=title_text,
                        summary=fallback_summary,
                        source="document_summary" if summary_text else "default",
                    )

                refreshed_asset_summaries[candidate_id] = entry
                asset_items.append(
                    {
                        "asset_id": candidate_id,
                        "title": entry["title"],
                        "type_label": entry["type_label"],
                        "summary": entry["summary"],
                    }
                )
                continue

            if status is None or status.status != AssetStatus.COMPLETED:
                continue

            cached_item = self._get_cached_asset_summary(asset_summary_map, status)
            if cached_item is not None:
                entry = self._build_asset_summary_entry(
                    status,
                    cached_item["summary"],
                    source=asset_summary_map.get(candidate_id, {}).get("source") or "cache",
                    title_override=cached_item["title"],
                )
                refreshed_asset_summaries[candidate_id] = entry
                asset_items.append(cached_item)
                continue

            fallback_entry = asset_summary_map.get(candidate_id, {})
            title_text = normalize_text(fallback_entry.get("title")) or self._extract_asset_title(
                status
            )
            summary_text = normalize_text(fallback_entry.get("summary")) or (
                f"与{title_text}相关的内容已完成索引，可用于知识库检索与问答。"
            )
            entry = self._build_asset_summary_entry(
                status,
                summary_text,
                source=fallback_entry.get("source") or "default",
                title_override=title_text,
            )
            refreshed_asset_summaries[candidate_id] = entry
            asset_items.append(
                {
                    "asset_id": candidate_id,
                    "title": entry["title"],
                    "type_label": entry["type_label"],
                    "summary": entry["summary"],
                }
            )

        if asset_items:
            overview = await self._agenerate_knowledgebase_overview(
                workspace=knowledgebase_id,
                asset_items=asset_items,
            )
            document_count = sum(1 for item in asset_items if item["type_label"] != "视频")
            video_count = sum(1 for item in asset_items if item["type_label"] == "视频")
            knowledgebase_summary = compose_knowledgebase_summary(
                document_count=document_count,
                video_count=video_count,
                overview=overview,
                asset_lines=[
                    build_asset_line(item["type_label"], item["title"], item["summary"])
                    for item in asset_items
                ],
                max_chars=max(
                    1,
                    int(getattr(self.config, "knowledgebase_summary_max_chars", 8000) or 8000),
                ),
            )
        else:
            knowledgebase_summary = EMPTY_KNOWLEDGEBASE_SUMMARY

        metadata = {
            **existing_metadata,
            "asset_summaries": refreshed_asset_summaries,
            "asset_count": len(active_asset_ids),
            "overview": overview if asset_items else "",
            "summary_updated_at": time.time(),
        }
        record = await self.aupsert_knowledgebase_info(
            knowledgebase_id,
            name=(existing.name if existing else knowledgebase_id),
            asset_ids=active_asset_ids,
            summarization=knowledgebase_summary,
            metadata=metadata,
        )
        logger.info(
            "HyperRAGV2: knowledgebase summary updated for %s asset_count=%s has_summary=%s",
            knowledgebase_id,
            len(active_asset_ids),
            bool(knowledgebase_summary),
        )
        return record

    async def _synchronize_knowledge_discovery_catalog(self, record: KnowledgebaseInfo) -> None:
        storage = await self._get_knowledge_discovery_catalog_storage()
        async with storage.library_transaction(record.id) as transaction:
            latest = await get_knowledgebase_info_storage().with_db(transaction.db).get(record.id)
            if latest is None:
                await transaction.delete_library(record.id)
            else:
                await transaction.synchronize_library(*self._discovery_catalog_snapshot(latest))

    @staticmethod
    def _discovery_catalog_snapshot(
        record: KnowledgebaseInfo,
    ) -> tuple[KnowledgeDiscoveryLibrary, list[KnowledgeDiscoveryAsset]]:
        """Project only title/summary metadata, never document contents or model output."""
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        summaries = HyperRAGV2._normalize_asset_summary_map(metadata)
        overview = normalize_text(metadata.get("overview")) or record.summarization
        active_asset_ids = list(
            dict.fromkeys(str(asset_id).strip() for asset_id in record.asset_ids if asset_id)
        )
        updated_at = float(record.updated_at)
        assets: list[KnowledgeDiscoveryAsset] = []
        document_count = 0
        video_count = 0
        for asset_id in active_asset_ids:
            entry = summaries.get(asset_id, {})
            title = normalize_text(entry.get("title")) or asset_id
            asset_type = normalize_text(entry.get("type_label")) or "文档"
            summary = normalize_text(entry.get("summary"))
            marker = normalize_text(entry.get("status_updated_at"))
            try:
                status_updated_at = float(marker) if marker else None
            except ValueError:
                status_updated_at = None
            if asset_type == "视频":
                video_count += 1
            else:
                document_count += 1
            assets.append(
                KnowledgeDiscoveryAsset(
                    library_id=record.id,
                    asset_id=asset_id,
                    title=title,
                    asset_type=asset_type,
                    summary=summary,
                    normalized_search_text=normalize_search_text(title, asset_type, summary),
                    is_active=True,
                    status_updated_at=status_updated_at,
                    updated_at=updated_at,
                )
            )

        library = KnowledgeDiscoveryLibrary(
            library_id=record.id,
            title=normalize_text(record.name) or record.id,
            overview=overview,
            asset_count=len(assets),
            document_count=document_count,
            video_count=video_count,
            normalized_search_text=normalize_search_text(record.name or record.id, overview),
            updated_at=updated_at,
        )
        return library, assets

    async def _get_knowledge_discovery_catalog_storage(self):
        storage = getattr(self, "_knowledge_discovery_catalog_storage", None)
        if storage is not None:
            return storage
        await self._ensure_core_runtime()
        return self._knowledge_discovery_catalog_storage

    @staticmethod
    def _normalize_discovery_library_ids(
        library_ids: list[str] | None,
        library_id: str | None,
    ) -> list[str] | None:
        if library_id is not None and library_ids is not None:
            raise ValueError("Specify either library_ids or library_id, not both")
        if library_id is not None:
            normalized = str(library_id).strip()
            return [normalized] if normalized else []
        if library_ids is None:
            return None
        if isinstance(library_ids, str):
            raise ValueError("library_ids must be a list of library IDs")
        return list(
            dict.fromkeys(normalized for item in library_ids if (normalized := str(item).strip()))
        )

    def _discovery_catalog_query_concurrency(self) -> int:
        config = getattr(self, "config", None)
        return max(
            1,
            int(getattr(config, "knowledge_discover_catalog_query_concurrency", 4) or 4),
        )

    def _discovery_asset_match_library_limit(self) -> int:
        config = getattr(self, "config", None)
        return max(
            1,
            int(getattr(config, "knowledge_discover_asset_match_library_limit", 20) or 20),
        )

    def _discovery_catalog_query_semaphore(self) -> asyncio.Semaphore:
        limit = self._discovery_catalog_query_concurrency()
        semaphore = getattr(self, "_knowledge_discovery_catalog_query_semaphore", None)
        configured_limit = getattr(
            self,
            "_knowledge_discovery_catalog_query_semaphore_limit",
            None,
        )
        if semaphore is None or configured_limit != limit:
            semaphore = asyncio.Semaphore(limit)
            self._knowledge_discovery_catalog_query_semaphore = semaphore
            self._knowledge_discovery_catalog_query_semaphore_limit = limit
        return semaphore

    async def _run_discovery_catalog_query(self, operation: Callable[[], Any]) -> Any:
        async with self._discovery_catalog_query_semaphore():
            result = operation()
            return await result if inspect.isawaitable(result) else result

    async def _map_discovery_catalog_queries(
        self,
        items: list[str],
        operation: Callable[[str], Any],
    ) -> list[Any]:
        if not items:
            return []

        results: list[Any] = [None] * len(items)
        next_index = 0

        async def worker() -> None:
            nonlocal next_index
            while next_index < len(items):
                index = next_index
                next_index += 1
                item = items[index]
                results[index] = await self._run_discovery_catalog_query(lambda: operation(item))

        worker_count = min(self._discovery_catalog_query_concurrency(), len(items))
        await asyncio.gather(*(worker() for _ in range(worker_count)))
        return results

    async def adiscover_library_matches(
        self,
        query: str,
        *,
        library_ids: list[str] | None = None,
        library_id: str | None = None,
        top_k: int | None = None,
        asset_limit: int | None = None,
    ) -> dict[str, Any]:
        """Return lexical catalog matches grouped by library without invoking embeddings."""
        limit = max(1, int(top_k)) if top_k is not None else None
        normalized_query = normalize_search_text(query)
        if str(query or "").strip():
            query_terms(query)
        scoped_ids = self._normalize_discovery_library_ids(library_ids, library_id)
        storage = await self._get_knowledge_discovery_catalog_storage()
        await self._wait_for_knowledge_discovery_catalog()

        if not normalized_query:
            if scoped_ids is None:
                libraries = await self._run_discovery_catalog_query(storage.list_libraries)
            else:
                libraries = [
                    library
                    for library in await self._map_discovery_catalog_queries(
                        scoped_ids,
                        storage.get_library,
                    )
                    if library is not None
                ]
            if limit is not None:
                libraries = libraries[:limit]
            return {
                "query": str(query or ""),
                "library_matches": [
                    {"library": library_to_dict(library), "asset_matches": []}
                    for library in libraries
                ],
            }

        per_library_limit = max(1, int(asset_limit or 3))
        libraries_by_id: dict[str, Any] = {}
        ordered_library_ids: list[str] = []
        library_hit_ids: set[str] = set()

        def add_library(candidate: Any) -> None:
            if candidate.library_id not in libraries_by_id:
                libraries_by_id[candidate.library_id] = candidate
                ordered_library_ids.append(candidate.library_id)

        scope = {} if scoped_ids is None else {"library_ids": scoped_ids}
        library_candidates, asset_library_ids = await asyncio.gather(
            self._run_discovery_catalog_query(
                lambda: storage.search_libraries(query, limit=limit, **scope)
            ),
            self._run_discovery_catalog_query(
                lambda: storage.search_asset_library_ids(query, limit=limit, **scope)
            ),
        )
        for library in await self._map_discovery_catalog_queries(
            asset_library_ids,
            storage.get_library,
        ):
            if library is not None:
                add_library(library)
        for candidate in library_candidates:
            add_library(candidate)
            library_hit_ids.add(candidate.library_id)

        candidate_library_ids = (
            ordered_library_ids[:limit] if limit is not None else ordered_library_ids
        )
        asset_match_library_ids = candidate_library_ids[
            : self._discovery_asset_match_library_limit()
        ]
        asset_matches_by_library = await self._map_discovery_catalog_queries(
            asset_match_library_ids,
            lambda candidate_id: storage.search_assets(
                query,
                library_id=candidate_id,
                limit=per_library_limit,
            ),
        )
        asset_matches_by_library_id = dict(
            zip(asset_match_library_ids, asset_matches_by_library, strict=True)
        )
        asset_library_id_set = set(asset_library_ids)
        matches: list[dict[str, Any]] = []
        for candidate_id in candidate_library_ids:
            if limit is not None and len(matches) >= limit:
                break
            library = libraries_by_id[candidate_id]
            asset_matches = asset_matches_by_library_id.get(candidate_id, [])
            library_matches_query = (
                not normalized_query or normalized_query in library.normalized_search_text
            )
            if (
                not asset_matches
                and candidate_id not in library_hit_ids
                and candidate_id not in asset_library_id_set
                and not library_matches_query
            ):
                continue
            matches.append(
                {
                    "library": library_to_dict(library),
                    "asset_matches": [asset_to_dict(asset) for asset in asset_matches],
                }
            )
        return {"query": str(query or ""), "library_matches": matches}

    async def adelete_knowledgebase_info(self, knowledgebase_id: str) -> None:
        catalog = await self._get_knowledge_discovery_catalog_storage()
        async with catalog.library_transaction(knowledgebase_id) as transaction:
            await get_knowledgebase_info_storage().with_db(transaction.db).delete(knowledgebase_id)
            await transaction.delete_library(knowledgebase_id)

    def delete_knowledgebase_info(self, *args, **kwargs) -> None:
        return asyncio.run(self.adelete_knowledgebase_info(*args, **kwargs))

    async def alist_assets(self, workspace: str) -> dict[str, AssetProcessingStatus]:
        st = get_asset_status_storage()
        return await st.get_all_status(workspace)

    def list_assets(self, *args, **kwargs) -> dict[str, AssetProcessingStatus]:
        return asyncio.run(self.alist_assets(*args, **kwargs))

    async def aget_asset_metadata(
        self,
        workspace: str,
        asset_id: str,
        *,
        include_chunk_count: bool = False,
    ) -> dict[str, Any] | None:
        st = get_asset_status_storage()
        try:
            status = await st.get_status(workspace, asset_id)
        except Exception:
            logger.exception(
                "Failed to read persisted asset metadata for workspace=%s asset_id=%s",
                workspace,
                asset_id,
            )
            status = None
        parse_mode = status.parse_mode if status and hasattr(status, "parse_mode") else None
        is_video: bool | None = (
            parse_mode in {"video", "sop_video"} if parse_mode is not None else None
        )
        stored_metadata_value = getattr(status, "metadata", None)
        stored_metadata = (
            dict(stored_metadata_value) if isinstance(stored_metadata_value, dict) else {}
        )
        metadata = dict(stored_metadata)
        metadata.pop("doc_type", None)
        legacy_metadata_keys = ["doc_type"] if "doc_type" in stored_metadata else []

        async def finalize_metadata() -> dict[str, Any]:
            if include_chunk_count and "chunk_nums" not in metadata:
                try:
                    metadata["chunk_nums"] = await self.aget_chunk_count(workspace, asset_id)
                except Exception:
                    logger.exception(
                        "Failed to derive chunk count for workspace=%s asset_id=%s",
                        workspace,
                        asset_id,
                    )
            await self._persist_asset_metadata_updates(
                st,
                workspace=workspace,
                asset_id=asset_id,
                stored_metadata=stored_metadata,
                metadata=metadata,
                remove_keys=legacy_metadata_keys,
            )
            return metadata

        if is_video is True:
            if "asset_type" in metadata and "video_duration" in metadata:
                return await finalize_metadata()
            metadata.setdefault("asset_type", "video")
            if "video_duration" in metadata:
                return await finalize_metadata()
        elif is_video is False:
            if "asset_type" in metadata and "page_count" in metadata:
                return await finalize_metadata()
            metadata.setdefault("asset_type", "doc")
            if "page_count" in metadata:
                return await finalize_metadata()

        try:
            if is_video is None:
                _, video_cache_dir = self._get_workspace_cache_dirs(workspace)
                video_asset, _, _ = await self._load_video_cache_snapshot(video_cache_dir, asset_id)
                is_video = video_asset is not None

            if is_video:
                # Try to read from sop_full.json (new unified approach)
                try:
                    sop_full = await self.aget_video_semantic(workspace, asset_id)
                except Exception:
                    logger.exception(
                        "Failed to read video semantic metadata for workspace=%s asset_id=%s",
                        workspace,
                        asset_id,
                    )
                    sop_full = None
                if sop_full:
                    # Look for video metadata in sop_full.json
                    # Expected structure: sop_full["video_metadata"] or sop_full["metadata"]["video_info"]
                    video_metadata: dict[str, Any] | None = None

                    # Check top-level video_metadata field
                    video_metadata = sop_full.get("video_metadata")

                    # Fall back to metadata.video_info
                    if not video_metadata:
                        meta = sop_full.get("metadata", {})
                        video_metadata = meta.get("video_info")

                    if isinstance(video_metadata, dict) and video_metadata:
                        duration = video_metadata.get("duration_sec")
                        fps = video_metadata.get("fps")
                        resolution = video_metadata.get("resolution")

                        if duration is not None:
                            metadata.setdefault("video_duration", float(duration))
                        if fps is not None and "fps" not in metadata:
                            metadata["fps"] = float(fps)
                        if (
                            isinstance(resolution, (list, tuple))
                            and len(resolution) == 2
                            and "resolution" not in metadata
                        ):
                            metadata["resolution"] = {
                                "width": int(resolution[0]),
                                "height": int(resolution[1]),
                            }

                if "video_duration" not in metadata:
                    # Fall back to index.json for backward compatibility
                    _, video_cache_dir = self._get_workspace_cache_dirs(workspace)
                    asset, _, _ = await self._load_video_cache_snapshot(video_cache_dir, asset_id)
                    if asset:
                        extra = asset.get("extra", {})
                        if not isinstance(extra, dict):
                            extra = {}
                        duration = extra.get("duration_sec")
                        fps = extra.get("fps")
                        resolution = extra.get("resolution")

                        if duration is not None:
                            metadata.setdefault("video_duration", float(duration))
                        if fps is not None and "fps" not in metadata:
                            metadata["fps"] = float(fps)
                        if (
                            isinstance(resolution, (list, tuple))
                            and len(resolution) == 2
                            and "resolution" not in metadata
                        ):
                            metadata["resolution"] = {
                                "width": int(resolution[0]),
                                "height": int(resolution[1]),
                            }
            else:
                metadata.setdefault("asset_type", "doc")
                mineru_cache_dir, _ = self._get_workspace_cache_dirs(workspace)
                asset, _ = await self._load_mineru_cache_snapshot(mineru_cache_dir, asset_id)
                if asset:
                    page_info = asset.get("page_info", {})
                    if not isinstance(page_info, dict):
                        page_info = {}
                    total_pages = page_info.get("total_pages")
                    if total_pages is None:
                        extra = asset.get("extra", {})
                        if isinstance(extra, dict):
                            total_pages = extra.get("total_pages")
                    if total_pages is not None:
                        metadata.setdefault("page_count", int(total_pages))

            return await finalize_metadata()
        except Exception:
            logger.exception(f"Error getting asset metadata for asset_id={asset_id}")
        return None

    @staticmethod
    async def _persist_asset_metadata_updates(
        storage: Any,
        *,
        workspace: str,
        asset_id: str,
        stored_metadata: dict[str, Any],
        metadata: dict[str, Any],
        remove_keys: list[str] | None = None,
    ) -> None:
        updates = {
            key: value for key, value in metadata.items() if stored_metadata.get(key) != value
        }
        keys_to_remove = [key for key in (remove_keys or []) if key in stored_metadata]
        if not updates and not keys_to_remove:
            return
        try:
            if keys_to_remove:
                await storage.update_metadata(
                    workspace,
                    asset_id,
                    updates,
                    remove_keys=keys_to_remove,
                )
            else:
                await storage.update_metadata(workspace, asset_id, updates)
        except Exception:
            logger.exception(
                "Failed to persist generated asset metadata for workspace=%s asset_id=%s",
                workspace,
                asset_id,
            )

    def get_asset_metadata(self, *args, **kwargs) -> dict[str, Any] | None:
        return asyncio.run(self.aget_asset_metadata(*args, **kwargs))

    def _get_video_semantic_paths(
        self, workspace: str, asset_id: str, parse_mode: str | None
    ) -> tuple[Path, list[Path]]:
        """Get canonical write and legacy read paths for the selected video mode."""
        from hyperrag.utils.segment_tools import video_semantic_paths

        _, video_cache_dir = self._get_workspace_cache_dirs(workspace)
        root = video_cache_dir / str(asset_id)
        return video_semantic_paths(root, parse_mode)

    @staticmethod
    def _validate_video_semantic_payload(
        payload: dict[str, Any], *, semantic_type: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("semantic_data must be a dict")
        if semantic_type not in {"video", "sop_video"}:
            raise ValueError(f"unsupported semantic_type: {semantic_type}")

        normalized = dict(payload)
        metadata = normalized.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("semantic_data.metadata must be a dict")
        if metadata.get("semantic_type") != semantic_type:
            raise ValueError(f"semantic_data.metadata.semantic_type must be {semantic_type}")
        sources = metadata.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("semantic_data.metadata.sources must be a non-empty list")

        segments = normalized.get("segments")
        if not isinstance(segments, list):
            raise ValueError("semantic_data.segments must be a list")
        segment_ids: set[str] = set()
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("semantic_data.segments entries must be dict")
            segment_id = str(segment.get("segment_id") or "")
            if not segment_id or segment_id in segment_ids:
                raise ValueError("semantic_data.segments must have unique segment_id")
            segment_ids.add(segment_id)
            if not segment.get("video_id"):
                raise ValueError("semantic_data.segments must include video_id")
            time_range = segment.get("time_range")
            if not isinstance(time_range, list) or len(time_range) != 2:
                raise ValueError("semantic_data.segments must include [begin, end] time_range")
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in time_range
                )
                or time_range[1] < time_range[0]
            ):
                raise ValueError("semantic_data.segments time_range must be finite and ordered")
            data = segment.get("data")
            if not isinstance(data, dict):
                raise ValueError("semantic_data.segments must include data dict")
            required_data_types = {
                "caption": str,
                "segment_background": str,
                "asr": dict,
                "visual_text": dict,
                "objects": list,
                "actions": list,
                "state_changes": list,
                "uncertainty": list,
                "meta": dict,
            }
            for field, expected_type in required_data_types.items():
                if field not in data:
                    raise ValueError(f"semantic_data.segments[].data missing {field}")
                if not isinstance(data[field], expected_type):
                    raise ValueError(
                        f"semantic_data.segments[].data.{field} must be {expected_type.__name__}"
                    )
            optional_data_types = {
                "brief_action": str,
                "detailed_action": str,
                "concepts": list,
                "focus_observations": list,
            }
            for field, expected_type in optional_data_types.items():
                if field in data and not isinstance(data[field], expected_type):
                    raise ValueError(
                        f"semantic_data.segments[].data.{field} must be {expected_type.__name__}"
                    )

        instructions = normalized.get("instructions")
        if semantic_type == "video":
            if "instructions" in normalized:
                raise ValueError("plain video semantic_data must not include instructions")
        else:
            if not isinstance(instructions, list):
                raise ValueError("sop_video semantic_data.instructions must be a list")
            for step in instructions:
                if not isinstance(step, dict):
                    raise ValueError("semantic_data.instructions entries must be dict")
                action_flow = step.get("action_flow")
                if not isinstance(action_flow, list):
                    raise ValueError("semantic_data.instructions[].action_flow must be a list")
                for action in action_flow:
                    if not isinstance(action, dict):
                        raise ValueError(
                            "semantic_data.instructions[].action_flow[] must be a dict"
                        )
                    if "segment_ids" not in action:
                        raise ValueError(
                            "semantic_data.instructions[].action_flow[].segment_ids is required"
                        )
                    action_segment_ids = action["segment_ids"]
                    if not isinstance(action_segment_ids, list):
                        raise ValueError(
                            "semantic_data.instructions[].action_flow[].segment_ids must be a list"
                        )
                    unknown_ids = [
                        str(segment_id)
                        for segment_id in action_segment_ids
                        if str(segment_id) not in segment_ids
                    ]
                    if unknown_ids:
                        raise ValueError(f"unknown action segment_ids: {unknown_ids}")
                    references = action.get("references", [])
                    if not isinstance(references, list):
                        raise ValueError(
                            "semantic_data.instructions[].action_flow[].references must be a list"
                        )
                    if any(
                        isinstance(reference, dict) and reference.get("type") == "video"
                        for reference in references
                    ):
                        raise ValueError("video references must use action.segment_ids")

        annotations = normalized.get("annotations", [])
        if annotations is None:
            annotations = []
        if not isinstance(annotations, list):
            raise ValueError("semantic_data.annotations must be a list")
        for ann in annotations:
            if not isinstance(ann, dict):
                raise ValueError("semantic_data.annotations entries must be dict")
            required_keys = {
                "annotation_id",
                "type",
                "video_id",
                "time_range",
                "text_description",
                "data",
            }
            missing = [key for key in required_keys if key not in ann]
            if missing:
                raise ValueError(f"annotation missing required keys: {missing}")
            time_range = ann.get("time_range")
            if not isinstance(time_range, list) or len(time_range) != 2:
                raise ValueError("annotation.time_range must be [begin, end]")

        normalized.setdefault("document_control", [])
        normalized.setdefault("groups", [])
        normalized["annotations"] = annotations
        return normalized

    async def aget_video_semantic(self, workspace: str, asset_id: str) -> dict[str, Any] | None:
        """
        读取普通视频或 SOP 视频的语义产物，优先当前命名并兼容历史文件。

        """
        st = get_asset_status_storage()
        status = await st.get_status(workspace, asset_id)
        if status is None:
            return None

        parse_mode = getattr(status, "parse_mode", None)
        if parse_mode not in {"video", "sop_video"}:
            return None

        _, candidates = self._get_video_semantic_paths(workspace, asset_id, parse_mode)
        sop_full_path = next((path for path in candidates if path.exists()), None)
        if sop_full_path is None:
            logger.warning(
                f"sop-full.json not found for asset_id={asset_id}, parse_mode={parse_mode}"
            )
            return None

        try:
            return json.loads(sop_full_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(f"Failed to read sop-full.json for asset_id={asset_id}")
            return None

    def get_video_semantic(self, *args, **kwargs) -> dict[str, Any] | None:
        return asyncio.run(self.aget_video_semantic(*args, **kwargs))

    async def aget_video_global_summary(self, workspace: str, asset_id: str) -> str:
        payload = await self.aget_video_semantic(workspace, asset_id)
        if not isinstance(payload, dict):
            raise ValueError(f"sop-full.json not found for asset {asset_id}")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("sop-full.json metadata is invalid")
        raw_summary = metadata.get("global_summary")
        if raw_summary is None:
            return ""
        return str(raw_summary).strip()

    def get_video_global_summary(self, *args, **kwargs) -> str:
        return asyncio.run(self.aget_video_global_summary(*args, **kwargs))

    async def aupdate_video_global_summary(
        self,
        workspace: str,
        asset_id: str,
        global_summary: str,
        request_id: str | None = None,
        global_context: dict[str, Any] | None = None,
    ) -> str:
        from actio.global_context import normalize_global_context

        normalized_summary = str(global_summary or "").strip()
        if not normalized_summary:
            raise ValueError("global_summary must not be empty")
        normalized_context = (
            normalize_global_context(global_context) if global_context is not None else None
        )
        st = get_asset_status_storage()
        status = await st.get_status(workspace, asset_id)
        if status is None:
            raise ValueError(f"asset not found: {asset_id}")
        parse_mode = str(getattr(status, "parse_mode", "") or "").strip().lower()
        if parse_mode != "video":
            raise ValueError(f"asset {asset_id} is not plain video parse mode: {parse_mode}")

        sop_full_path, candidates = self._get_video_semantic_paths(workspace, asset_id, parse_mode)
        source_path = next((path for path in candidates if path.exists()), None)
        if source_path is None:
            raise ValueError(f"sop-full.json not found for asset {asset_id}")
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("sop-full.json payload is invalid")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            payload["metadata"] = metadata
        pending = metadata.get("global_summary_submission")
        if isinstance(pending, dict) and pending.get("status") == "unknown":
            raise ValueError(f"请先核查未确认的索引任务 {pending.get('job_id', '')}，不要重复提交")
        original_text = (
            sop_full_path.read_text(encoding="utf-8") if sop_full_path.exists() else None
        )
        metadata["global_summary"] = normalized_summary
        # Omitted context from legacy clients must never clear existing attention items.
        if normalized_context is not None:
            metadata["global_context"] = normalized_context
        metadata["update_timestamp"] = datetime.datetime.now().isoformat()
        updated_text = json.dumps(payload, ensure_ascii=False, indent=2)
        _write_video_summary_text(sop_full_path, updated_text, source_path=source_path)
        try:
            return await self.aupdate_video_index(
                workspace=workspace,
                asset_id=asset_id,
                edited_sop_full_path=str(sop_full_path),
                request_id=request_id,
            )
        except VideoUpdateSubmissionUnknown as exc:
            # A worker may already be consuming the new content. Preserve it and
            # retain a durable reconciliation handle rather than resubmitting.
            if sop_full_path.read_text(encoding="utf-8") == updated_text:
                metadata["global_summary_submission"] = {"status": "unknown", "job_id": exc.job_id}
                _write_video_summary_text(
                    sop_full_path, json.dumps(payload, ensure_ascii=False, indent=2)
                )
            raise
        except BaseException:
            # Before scheduler submission, restoring is safe; never overwrite
            # a concurrent edit or a worker's completed output.
            if sop_full_path.exists() and sop_full_path.read_text(encoding="utf-8") == updated_text:
                if original_text is None:
                    sop_full_path.unlink()
                else:
                    _write_video_summary_text(sop_full_path, original_text)
            raise

    def update_video_global_summary(self, *args, **kwargs) -> str:
        return asyncio.run(self.aupdate_video_global_summary(*args, **kwargs))

    async def aupdate_video_index(
        self,
        *,
        workspace: str,
        asset_id: str,
        edited_sop_full_path: str,
        request_id: str | None = None,
    ) -> str:
        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        assert self._queue is not None

        st = get_asset_status_storage()
        asset_status = await st.get_status(workspace, asset_id)
        if asset_status is None:
            raise ValueError(f"asset not found: {asset_id}")
        parse_mode = str(getattr(asset_status, "parse_mode", "") or "").strip().lower()
        if parse_mode != "video":
            raise ValueError(f"asset {asset_id} is not plain video parse mode: {parse_mode}")

        asset_path = str(getattr(asset_status, "file_path", "") or "").strip()
        if not asset_path:
            raise ValueError(f"asset file_path missing for {asset_id}")

        edited_path = Path(edited_sop_full_path)
        if not edited_path.is_file():
            raise ValueError(f"edited sop-full.json not found: {edited_path}")
        payload = json.loads(edited_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("edited sop-full.json payload is invalid")
        metadata = payload.get("metadata")
        title = ""
        if isinstance(metadata, dict):
            title = str(metadata.get("title") or "").strip()

        params = {
            "job_type": "video_update",
            "parse_mode": "video",
            "edited_sop_full_path": str(edited_path),
            **({"title": title} if title else {}),
        }
        job, tasks = JobBuilder.from_asset(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=asset_path,
            params=params,
            callbacks={},
            request_id=request_id,
        )
        try:
            await st.set_status(
                workspace,
                asset_id,
                AssetStatus.QUEUED,
                progress=0.0,
                parse_mode="video",
                file_path=asset_path,
                file_data=None,
            )
        except BaseException:
            # No scheduler call has happened yet. Restore a possibly partially
            # applied status transition; failures remain visible to the caller.
            await st.set_status(
                workspace,
                asset_id,
                asset_status.status,
                progress=asset_status.progress,
                parse_mode=asset_status.parse_mode,
                file_path=asset_status.file_path,
                file_data=asset_status.file_data,
            )
            raise
        try:
            job_id = await self.scheduler.submit_job(job, tasks)
        except BaseException as exc:
            raise VideoUpdateSubmissionUnknown(job.job_id) from exc
        job_by_asset_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"
        try:
            await self._queue.redis.set(job_by_asset_key, job_id, ex=86400)
        except Exception as e:
            logger.warning(f"Failed to record video_update job_by_asset mapping: {e}")
        except BaseException as exc:
            # Submission succeeded. Cancellation here must not roll back metadata
            # that a worker may already be reading.
            raise VideoUpdateSubmissionUnknown(job_id) from exc
        return job_id

    def update_video_index(self, *args, **kwargs) -> str:
        return asyncio.run(self.aupdate_video_index(*args, **kwargs))

    async def aupdate_video_semantic(
        self, workspace: str, asset_id: str, semantic_data: dict[str, Any]
    ) -> bool:
        """
        更新 SOP 视频语义文件（sop-full.json）。

        保存语义数据后，如果 segment payload 发生变化，则触发索引重建。
        """
        st = get_asset_status_storage()
        status = await st.get_status(workspace, asset_id)
        if status is None:
            raise ValueError(f"asset not found: {asset_id}")

        parse_mode = getattr(status, "parse_mode", None)
        if parse_mode not in {"video", "sop_video"}:
            raise ValueError(f"asset {asset_id} is not video parse mode: {parse_mode}")

        old_payload = await self.aget_video_semantic(workspace, asset_id)
        old_segment_payloads = self._build_video_segment_payloads(
            asset_id=asset_id,
            sop_full_data=old_payload if isinstance(old_payload, dict) else {},
            source_parser=parse_mode,
        )

        payload = self._validate_video_semantic_payload(
            dict(semantic_data), semantic_type=parse_mode
        )
        sop_full_path, _ = self._get_video_semantic_paths(workspace, asset_id, parse_mode)
        sop_full_path.parent.mkdir(parents=True, exist_ok=True)

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            payload["metadata"] = metadata
        metadata.setdefault("version", metadata.get("version") or "generated")
        metadata.setdefault("status", metadata.get("status") or "draft")
        metadata["semantic_version"] = int(metadata.get("semantic_version") or 0) + 1
        metadata["update_timestamp"] = datetime.datetime.now().isoformat()

        sop_full_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await self._rebuild_video_semantic_index_if_changed(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=str(getattr(status, "file_path", "") or sop_full_path),
            parse_mode=parse_mode,
            payload=payload,
            old_segment_payloads=old_segment_payloads,
            new_segment_payloads=self._build_video_segment_payloads(
                asset_id=asset_id,
                sop_full_data=payload,
                source_parser=parse_mode,
            ),
        )
        return True

    def update_video_semantic(self, *args, **kwargs) -> bool:
        return asyncio.run(self.aupdate_video_semantic(*args, **kwargs))

    async def _rebuild_video_semantic_index_if_changed(
        self,
        *,
        workspace: str,
        asset_id: str,
        asset_path: str,
        parse_mode: str,
        payload: dict[str, Any],
        old_segment_payloads: dict[str, str],
        new_segment_payloads: dict[str, str],
    ) -> str | None:
        changed_segment_ids = {
            sid
            for sid, new_payload in new_segment_payloads.items()
            if old_segment_payloads.get(sid) != new_payload
        }
        changed_segment_ids.update(set(old_segment_payloads) - set(new_segment_payloads))
        if not changed_segment_ids:
            return None

        if not self._initialized:
            await self.initialize()
        assert self.scheduler is not None
        assert self._queue is not None

        metadata = payload.get("metadata") or {}
        video_source = get_primary_video_source(payload)
        video_source_id = video_source.get("source_id") or asset_id
        segments = build_video_segments(
            asset_id=asset_id,
            sop_full_data=payload,
            source_parser=parse_mode,
        )
        asset = {
            "doc_id": asset_id,
            "file_path": asset_path,
            "doc_type": parse_mode,
            "modality": "video",
            "title": metadata.get("title", ""),
            "extra": {
                "asset_id": asset_id,
                "video_id": video_source_id,
                "source_id": video_source_id,
                "source_url": video_source.get("source_url", ""),
                "source_parser": parse_mode,
                "sop_title": metadata.get("title", ""),
                "sop_id": metadata.get("sop_id", ""),
            },
        }

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job_name = parse_mode if parse_mode in {"video", "sop_video"} else "video"
        job = JobInstance(
            job_id=job_id,
            job_name=job_name,
            workspace=workspace,
            asset_id=asset_id,
            asset_path=asset_path,
            stages=["delete_asset", "kg_index", "bm25_index"],
            metadata={
                "parse_mode": parse_mode,
                "reason": "video_semantic_update",
                "changed_segment_ids": sorted(changed_segment_ids),
            },
        )

        common_metadata = {
            "asset_path": asset_path,
            "parse_mode": parse_mode,
            "asset": asset,
            "segments": segments,
            "sop_full": payload,
            "reason": "video_semantic_update",
            "changed_segment_ids": sorted(changed_segment_ids),
        }
        delete_task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            task_type=TaskType.DELETE_ASSET,
            workspace=workspace,
            asset_id=asset_id,
            status=TaskStatus.QUEUED,
            resource=self._get_handler_resource(TaskType.DELETE_ASSET),
            job_id=job_id,
            job_name=job_name,
            stage_index=0,
            depends_on=[],
            metadata={
                **common_metadata,
                "skip_cache_cleanup": True,
            },
        )
        index_task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            task_type=TaskType.KG_INDEX,
            workspace=workspace,
            asset_id=asset_id,
            status=TaskStatus.PENDING,
            resource=self._get_handler_resource(TaskType.KG_INDEX),
            job_id=job_id,
            job_name=job_name,
            stage_index=1,
            depends_on=[delete_task.task_id],
            metadata=common_metadata,
        )
        bm25_task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            task_type=TaskType.BM25_INDEX,
            workspace=workspace,
            asset_id=asset_id,
            status=TaskStatus.PENDING,
            resource=self._get_handler_resource(TaskType.BM25_INDEX),
            job_id=job_id,
            job_name=job_name,
            stage_index=2,
            depends_on=[index_task.task_id],
            metadata=common_metadata,
        )
        tasks = [delete_task, index_task, bm25_task]
        for task in tasks:
            job.task_ids.append(task.task_id)

        await self.scheduler.submit_job(job, tasks)

        job_by_asset_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"
        try:
            await self._queue.redis.set(job_by_asset_key, job_id, ex=86400)
        except Exception as e:
            logger.warning(f"Failed to record semantic reindex job_by_asset mapping: {e}")

        logger.info(
            f"Submitted video semantic reindex job for asset {asset_id}: "
            f"{job_id}, changed_segment_ids={changed_segment_ids}"
        )
        return job_id

    @staticmethod
    def _build_video_segment_payloads(
        *,
        asset_id: str,
        sop_full_data: dict[str, Any],
        source_parser: str,
    ) -> dict[str, str]:
        payloads: dict[str, str] = {}
        for segment in build_video_segments(
            asset_id=asset_id,
            sop_full_data=sop_full_data,
            source_parser=source_parser,
        ):
            segment_id = segment.get("segment_id")
            if not segment_id:
                continue
            payloads[str(segment_id)] = json.dumps(
                segment,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return payloads

    async def adelete_asset(
        self, workspace: str, asset_id: str, request_id: str | None = None
    ) -> str:
        """
        删除资源及其所有相关数据（通过任务队列异步执行）

        Args:
            workspace: 工作空间名称
            asset_id: 资源ID

        Returns:
            str: job_id，可用于查询删除任务状态
        """
        if not self._initialized:
            await self.initialize()
        assert self._queue is not None
        assert self.scheduler is not None

        job_by_asset_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"

        # 获取 parse_mode 以便 handler 知道如何清理缓存
        parse_mode = None
        st = get_asset_status_storage()
        try:
            status = await st.get_status(workspace, asset_id)
            if status:
                parse_mode = status.parse_mode if status and hasattr(status, "parse_mode") else None

                existing_job_id = await self._queue.redis.get(job_by_asset_key)
                if status.status in {AssetStatus.QUEUED, AssetStatus.DELETING} and existing_job_id:
                    existing_job = await self._queue.get_job(existing_job_id)
                    if (
                        existing_job
                        and existing_job.job_name == "delete"
                        and existing_job.status not in {"completed", "failed", "cancelled"}
                    ):
                        logger.info(
                            "Reuse in-flight delete job for asset %s: %s",
                            asset_id,
                            existing_job_id,
                        )
                        return existing_job_id
        except Exception as e:
            logger.debug(f"Failed to get parse_mode for {asset_id}: {e}")

        # 先设置 asset_status 为 Queued（与 aadd_asset 逻辑一致）
        await st.set_status(
            workspace,
            asset_id,
            AssetStatus.QUEUED,
            progress=0.0,
            parse_mode=parse_mode,
        )

        # 构造删除 Job
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = JobInstance(
            job_id=job_id,
            job_name="delete",
            workspace=workspace,
            asset_id=asset_id,
            asset_path="",  # Deletion doesn't need asset_path
            stages=["delete_asset"],
            metadata={
                **({"parse_mode": parse_mode} if parse_mode else {}),
                **({"_request_id": request_id} if request_id else {}),
            },
        )

        # 构造删除任务
        task = Task(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            task_type=TaskType.DELETE_ASSET,
            workspace=workspace,
            asset_id=asset_id,
            status=TaskStatus.QUEUED,
            resource=TaskResource(slots=1, priority=3),  # Higher priority
            priority=3,
            job_id=job_id,
            job_name="delete",
            stage_index=0,
            depends_on=[],
            metadata={
                **({"parse_mode": parse_mode} if parse_mode else {}),
                **({"_request_id": request_id} if request_id else {}),
            },
        )

        job.task_ids.append(task.task_id)

        # 提交到调度器
        await self.scheduler.submit_job(job, [task])

        try:
            await self._queue.redis.set(job_by_asset_key, job_id, ex=86400)
        except Exception as e:
            logger.warning(f"Failed to record delete job_by_asset mapping: {e}")

        logger.info(f"Delete job submitted for asset {asset_id}: {job_id}")
        return job_id

    def delete_asset(self, *args, **kwargs) -> str:
        """同步删除资源接口，返回 job_id"""
        return asyncio.run(self.adelete_asset(*args, **kwargs))

    # -------------------- 对外：chunk（返回类型与 hyperrag.py 一致） --------------------
    async def aget_chunk_by_id(self, workspace: str, chunk_id: str) -> dict[str, Any] | None:
        chunks = await self._get_kg_chunks_by_ids(workspace, [chunk_id])
        return chunks.get(str(chunk_id))

    async def aget_chunks_by_ids(
        self, workspace: str, chunk_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """
        Bulk fetch chunk basics by ids to reduce per-chunk DB roundtrips.

        Returns a mapping: chunk_id -> chunk_info (same structure as aget_chunk_by_id()).
        Missing chunks are omitted from the mapping.
        """
        if not chunk_ids:
            return {}

        return await self._get_kg_chunks_by_ids(workspace, chunk_ids)

    def get_chunk_by_id(self, *args, **kwargs) -> dict[str, Any] | None:
        return asyncio.run(self.aget_chunk_by_id(*args, **kwargs))

    def get_chunks_by_ids(self, *args, **kwargs) -> dict[str, dict[str, Any]]:
        """批量获取 chunks（同步包装）"""
        return asyncio.run(self.aget_chunks_by_ids(*args, **kwargs))

    async def aget_chunk_count(self, workspace: str, file_id: str) -> int:
        kg_chunks = await self._get_kg_asset_chunks(workspace, file_id)
        return len(kg_chunks)

    def get_chunk_count(self, *args, **kwargs) -> int:
        return asyncio.run(self.aget_chunk_count(*args, **kwargs))

    async def aget_asset_chunks(
        self, workspace: str, file_id: str, page: dict[str, int] | None = None
    ) -> list[dict[str, Any]]:
        """
        获取文件的 chunk 列表（批量读取，metadata 已在 chunker 阶段写入）

        Args:
            workspace: 工作空间名称
            file_id: 文件ID
            page: 分页参数 {"start": int, "end": int}，None 表示获取全部

        Returns:
            chunk 列表，每个 chunk 包含完整的 metadata（已在入库时写入）
        """
        kg_chunks = await self._get_kg_asset_chunks(workspace, file_id)
        if page:
            start = page.get("start", 0)
            end = page.get("end", len(kg_chunks))
            return kg_chunks[start:end]
        return kg_chunks

    def get_asset_chunks(self, *args, **kwargs) -> list[dict[str, Any]]:
        return asyncio.run(self.aget_asset_chunks(*args, **kwargs))

    async def acreate_chunk(
        self,
        workspace: str,
        file_id: str,
        chunk_id: str,
        chunk_content: str,
        chunk_order: int | None = None,
    ) -> dict[str, Any]:
        existing = await self.aget_chunk_by_id(workspace, chunk_id)
        if existing is not None:
            raise ValueError(f"Chunk {chunk_id} already exists")
        status = await get_asset_status_storage().get_status(workspace, file_id)
        if status is None or not status.file_path:
            raise ValueError(f"Document {file_id} not found in asset status storage")
        chunks = await self._get_kg_asset_chunks(workspace, file_id)
        insert_pos = (
            max(0, min(int(chunk_order), len(chunks))) if chunk_order is not None else len(chunks)
        )
        experiment = await self._get_kg_experiment(workspace)
        await experiment.upsert_manual_chunk(
            chunk_id=chunk_id,
            file_path=status.file_path,
            content=chunk_content,
            order=insert_pos,
            metadata={"edited": False, "asset_id": file_id},
        )
        for index, chunk in enumerate(chunks[insert_pos:], start=insert_pos + 1):
            await experiment.upsert_manual_chunk(
                chunk_id=str(chunk["chunk_id"]),
                file_path=str(chunk["file_path"]),
                content=str(chunk["content"]),
                order=index,
                metadata=dict(chunk.get("metadata") or {}),
                segment_ids=list(chunk.get("segment_ids") or []),
            )

        # 集成 BM25 存储
        if self.bm25_storage_manager and chunk_content:
            try:
                content_tokenized = self._jieba_tokenize(chunk_content)
                bm25_storage = await self.bm25_storage_manager.get_storage(workspace)
                await bm25_storage.upsert(
                    workspace=workspace,
                    id=chunk_id,
                    full_doc_id=file_id,
                    chunk_order_index=insert_pos,
                    content=chunk_content,
                    content_tokenized=content_tokenized,
                    tokens=len(content_tokenized.split()),
                )
            except Exception as e:
                logger.exception(f"Failed to add BM25 chunk: {e}")

        return {
            "chunk_id": chunk_id,
            "context": "",
            "content": chunk_content,
            "tokens": len(chunk_content.split()),
            "full_doc_id": file_id,
            "order": insert_pos,
            "segment_ids": [],
            "metadata": {"edited": False},
        }

    def create_chunk(self, *args, **kwargs) -> dict[str, Any]:
        return asyncio.run(self.acreate_chunk(*args, **kwargs))

    async def aupdate_chunk(
        self, workspace: str, file_id: str, chunk_id: str, chunk_data: dict[str, Any]
    ) -> dict[str, Any]:
        existing = await self.aget_chunk_by_id(workspace, chunk_id)
        if not existing:
            raise ValueError(f"Chunk {chunk_id} not found")
        existing_metadata = dict(existing.get("metadata") or {})
        incoming_metadata = chunk_data.get("metadata")
        if isinstance(incoming_metadata, dict):
            existing_metadata.update(incoming_metadata)
        existing_metadata["edited"] = True
        new_content = str(chunk_data.get("content", existing.get("content", "")))
        order = int(chunk_data.get("chunk_order_index", existing.get("order", 0)))
        await (await self._get_kg_experiment(workspace)).upsert_manual_chunk(
            chunk_id=chunk_id,
            file_path=str(chunk_data.get("file_path") or existing.get("file_path") or ""),
            content=new_content,
            order=order,
            metadata=existing_metadata,
            segment_ids=list(existing.get("segment_ids") or []),
        )

        # 集成 BM25 存储
        if self.bm25_storage_manager and new_content:
            try:
                content_tokenized = self._jieba_tokenize(new_content)
                bm25_storage = await self.bm25_storage_manager.get_storage(workspace)
                await bm25_storage.upsert(
                    workspace=workspace,
                    id=chunk_id,
                    full_doc_id=file_id,
                    chunk_order_index=chunk_data.get("chunk_order_index", 0),
                    content=new_content,
                    content_tokenized=content_tokenized,
                    tokens=len(content_tokenized.split()),
                )
            except Exception as e:
                logger.exception(f"Failed to update BM25 chunk: {e}")

        return {
            "chunk_id": chunk_id,
            "content": new_content,
            "context": existing.get("context", ""),
            "tokens": chunk_data.get("tokens", 0),
            "full_doc_id": chunk_data.get("full_doc_id", file_id),
            "order": order,
            "metadata": existing_metadata,
        }

    def update_chunk(self, *args, **kwargs) -> dict[str, Any]:
        return asyncio.run(self.aupdate_chunk(*args, **kwargs))

    async def adelete_chunk(self, workspace: str, file_id: str, chunk_id: str) -> bool:
        existing = await self.aget_chunk_by_id(workspace, chunk_id)
        if not existing:
            logger.warning(f"Chunk {chunk_id} not found")
            return False
        experiment = await self._get_kg_experiment(workspace)
        await experiment.delete_manual_chunk(chunk_id=chunk_id)
        chunks = await self._get_kg_asset_chunks(workspace, file_id)
        for index, chunk in enumerate(chunks):
            await experiment.upsert_manual_chunk(
                chunk_id=str(chunk["chunk_id"]),
                file_path=str(chunk["file_path"]),
                content=str(chunk["content"]),
                order=index,
                metadata=dict(chunk.get("metadata") or {}),
                segment_ids=list(chunk.get("segment_ids") or []),
            )

        # 集成 BM25 存储
        if self.bm25_storage_manager:
            try:
                bm25_storage = await self.bm25_storage_manager.get_storage(workspace)
                await bm25_storage.delete(workspace=workspace, ids=[chunk_id])
            except Exception as e:
                logger.exception(f"Failed to delete BM25 chunk: {e}")

        return True

    def delete_chunk(self, *args, **kwargs) -> bool:
        return asyncio.run(self.adelete_chunk(*args, **kwargs))

    async def _load_mineru_cache_snapshot(
        self, mineru_cache_dir: Path, doc_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        asset: dict[str, Any] | None = None
        segments: list[dict[str, Any]] = []
        try:
            root = Path(mineru_cache_dir)
            auto_path = root / str(doc_id) / "vlm" / "index.json"
            if auto_path.exists():
                with open(auto_path, encoding="utf-8") as f:
                    data = json.load(f)
                asset = data.get("asset") or {}
                segments = list(data.get("segments") or [])
                return asset, segments
            candidates = list(root.rglob("index.json"))
            for idx_path in candidates:
                with open(idx_path, encoding="utf-8") as f:
                    data = json.load(f)
                a = data.get("asset") or {}
                if str(a.get("doc_id")) == str(doc_id):
                    asset = a
                    segments = list(data.get("segments") or [])
                    return asset, segments
        except Exception:
            logger.exception(f"Error loading mineru_cache snapshot for doc_id={doc_id}")
        return None, []

    async def _load_video_cache_snapshot(
        self, video_cache_dir: Path, doc_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        asset: dict[str, Any] | None = None
        segments: list[dict[str, Any]] = []
        transcript: list[dict[str, Any]] = []
        try:
            root = Path(video_cache_dir or ".")
            vc_dir = root / str(doc_id)
            index_path = vc_dir / "index.json"
            trans_path = vc_dir / "transcript.json"

            if not index_path.exists():
                for sub in root.glob("*/index.json"):
                    try:
                        with open(sub, encoding="utf-8") as f:
                            data = json.load(f)
                        a = data.get("asset") or {}
                        if str(a.get("doc_id")) == str(doc_id):
                            asset = a
                            segments = list(data.get("segments") or [])
                            tp = sub.parent / "transcript.json"
                            if tp.exists():
                                with open(tp, encoding="utf-8") as tf:
                                    transcript = json.load(tf)
                            return asset, segments, transcript
                    except Exception:
                        continue
                return None, [], []

            with open(index_path, encoding="utf-8") as f:
                data = json.load(f)
            asset = data.get("asset") or {}
            segments = list(data.get("segments") or [])

            if trans_path.exists():
                with open(trans_path, encoding="utf-8") as tf:
                    transcript = json.load(tf)
            return asset, segments, transcript
        except Exception:
            logger.exception(f"Error loading video_cache snapshot for doc_id={doc_id}")
        return None, [], []

    async def _load_sop_video_snapshot(
        self, video_cache_dir: Path, doc_id: str, source_parser: str = "sop_video"
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        """Load the selected video semantic artifact using semantic segment IDs.

        Both video modes read canonical IDs from top-level `segments`. SOP instructions
        reference those IDs through `action_flow[].segment_ids`.
        """
        try:
            from hyperrag.utils.segment_tools import video_semantic_paths

            root = Path(video_cache_dir or ".") / str(doc_id)
            _, candidates = video_semantic_paths(root, source_parser)

            sop_data: dict[str, Any] | None = None
            for candidate in candidates:
                if not candidate.exists():
                    continue
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    sop_data = payload
                    break

            if not isinstance(sop_data, dict):
                return None, [], []

            metadata = sop_data.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}

            video_source = get_primary_video_source(sop_data)
            source_id = str(video_source.get("source_id") or doc_id)
            source_url = str(video_source.get("source_url") or "")

            asset = {
                "doc_id": str(doc_id),
                "doc_type": source_parser,
                "modality": "video",
                "title": str(metadata.get("title") or ""),
                "extra": {
                    "asset_id": str(doc_id),
                    "video_id": source_id,
                    "source_id": source_id,
                    "source_url": source_url,
                    "source_parser": source_parser,
                    "sop_title": str(metadata.get("title") or ""),
                    "sop_id": str(metadata.get("sop_id") or ""),
                },
            }

            segments = build_video_segments(
                asset_id=str(doc_id),
                sop_full_data=sop_data,
                source_parser=source_parser,
            )

            transcript = sop_data.get("transcript")
            if not isinstance(transcript, list):
                transcript = []

            return asset, segments, transcript
        except Exception:
            logger.exception(f"Error loading sop_video snapshot for doc_id={doc_id}")
            return None, [], []

    # -------------------- 取消索引 --------------------
    async def acancel_asset(self, workspace: str, asset_id: str) -> bool:
        """
        取消资源索引（best-effort）

        Args:
            workspace: 工作空间名称
            asset_id: 资源ID

        Returns:
            是否成功取消（True 表示已取消或标记为取消）
        """
        if not self._initialized:
            await self.initialize()
        assert self._queue is not None
        assert self.scheduler is not None

        # 1. 读取 job_id
        job_by_asset_key = f"hyperrag:job_by_asset:{workspace}:{asset_id}"
        try:
            job_id = await self._queue.redis.get(job_by_asset_key)
            if not job_id:
                logger.warning(f"Job not found for asset: {workspace}/{asset_id}")
                return False
        except Exception as e:
            logger.error(f"Failed to read job_by_asset mapping: {e}")
            return False

        # 2. 读取 job 和 tasks
        try:
            job = await self._queue.get_job(job_id)
            if not job:
                logger.warning(f"Job data not found: {job_id}")
                return False

            # 如果 job 已经完成或失败，无需取消
            if job.status in {"completed", "failed", "cancelled"}:
                logger.info(f"Job already in terminal state: {job.status}")
                return job.status == "cancelled"

            # 3. 遍历所有 task，尝试取消
            cancelled_count = 0
            for task_id in job.task_ids:
                try:
                    task = await self._queue.get_task(task_id)
                    if not task:
                        continue

                    # 只处理未完成的任务
                    if str(task.status).upper() in {"COMPLETED", "FAILED", "CANCELLED"}:
                        continue

                    # 从队列中移除（如果在队列中）
                    if str(task.status).upper() == "QUEUED":
                        queue_key = f"hyperrag:queue:{task.task_type}"
                        try:
                            await self._queue.redis.zrem(queue_key, task.task_id)
                            logger.debug(f"Removed task from queue: {task.task_id}")
                        except Exception as e:
                            logger.warning(f"Failed to remove task from queue: {e}")

                    # 如果任务正在本进程的 scheduler 中运行，尝试取消协程
                    if self.scheduler and hasattr(self.scheduler, "_running_tasks"):
                        running_tasks = self.scheduler._running_tasks
                        if task.task_id in running_tasks:
                            try:
                                coro_task, _ = running_tasks[task.task_id]
                                coro_task.cancel()
                                logger.info(f"Cancelled running task: {task.task_id}")
                            except Exception as e:
                                logger.warning(f"Failed to cancel running task: {e}")

                    # 更新 task 状态为 cancelled
                    from .task.models import TaskStatus

                    task.status = TaskStatus.CANCELLED
                    await self._queue.update_task(task)
                    cancelled_count += 1

                except Exception as e:
                    logger.exception(f"Failed to cancel task {task_id}: {e}")

            # 4. 更新 job 状态为 cancelled
            job.status = "cancelled"
            await self._queue.update_job(job)

            # 5. 更新 AssetStatus 为 CANCELLED
            st = get_asset_status_storage()
            await st.set_status(workspace, asset_id, AssetStatus.CANCELLED, progress=0.0)

            logger.info(
                f"Asset indexing cancelled: {workspace}/{asset_id} "
                f"(job={job_id}, cancelled_tasks={cancelled_count})"
            )
            return True

        except Exception as e:
            logger.exception(f"Failed to cancel asset indexing: {e}")
            return False

    def cancel_asset(self, *args, **kwargs) -> bool:
        """取消资源索引（同步包装）"""
        return asyncio.run(self.acancel_asset(*args, **kwargs))

    # -------------------- 生命周期 --------------------
    async def shutdown(self):
        logger.info("Shutting down HyperRAGV2...")

        if self._post_compile_task:
            self._post_compile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._post_compile_task
            self._post_compile_task = None

        try:
            if self.scheduler:
                await self.scheduler.stop_heartbeat()
                await self.scheduler.stop()
        except Exception:
            logger.exception("Failed to stop scheduler; ignored")

        try:
            if self.bm25_storage_manager:
                await self.bm25_storage_manager.close_all()
        except Exception:
            logger.exception("Failed to close BM25 storages; ignored")

        try:
            async with self._kg_experiments_lock:
                experiments = list(self._kg_experiments.values())
                self._kg_experiments.clear()
            for experiment in experiments:
                try:
                    await experiment.close()
                except Exception:
                    logger.exception("Failed to close kg experiment; ignored")
        except Exception:
            logger.exception("Failed to close kg experiments; ignored")

        try:
            async with self._workspace_asset_status_cache_lock:
                self._workspace_asset_status_cache.clear()
        except Exception:
            logger.exception("Failed to clear workspace asset status cache; ignored")

        try:
            if self._queue:
                await self._queue.close()
        except Exception:
            logger.exception("Failed to close queue; ignored")

        # 关闭所有 status storages
        try:
            async with self._status_storages_lock:
                storages = list(self._status_storages.values())
                self._status_storages.clear()
            for st in storages:
                try:
                    await st.close()
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to close status storages; ignored")

        self._initialized = False
        self._core_initialized = False
        self._bm25_runtime_initialized = False
        self._task_runtime_initialized = False
        self._handlers_registered = False
        self._queue = None
        self.scheduler = None
        self.terminology_helper = None
        self.bm25_storage_manager = None
        self.artifact_store = None
        logger.info("HyperRAGV2 shutdown completed")
