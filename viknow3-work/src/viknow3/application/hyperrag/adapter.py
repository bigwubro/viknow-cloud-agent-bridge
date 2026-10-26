"""Thin HyperRAG adapter used by viknow2 APIs and tools."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import inspect
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from viknow3.application.hyperrag.config import AppConfig, materialize_hyperrag_config
from viknow3.application.hyperrag.evidence import Evidence, EvidencePart, EvidencePartKind, EvidenceTimeRange
from viknow3.application.hyperrag.knowledge_provider import (
    KnowledgeAssetDiscovery,
    KnowledgeLibrary,
    KnowledgeLibrarySearchResult,
)
from viknow3.application.hyperrag.knowledgebase_summary import (
    EMPTY_KNOWLEDGEBASE_SUMMARY,
    compose_summary_from_assets,
    infer_asset_title,
    infer_asset_type_label,
    normalize_text,
)
from viknow3.application.hyperrag.otel import ATTR_PHASE, ATTR_RUN_ID, start_span, start_span_sync
from viknow3.application.hyperrag.scheduler_context import get_source_id
from viknow3.application.hyperrag.table_evidence_source import bind_table_sources


def _text_file_as_single_page_pdf(path: Path) -> Path | None:
    """Give text assets a one-page PDF so document_inspect can render them."""
    target = path.with_name(path.name + ".inspect.pdf")
    try:
        if target.exists() and target.stat().st_mtime >= path.stat().st_mtime:
            return target
        import fitz

        document = fitz.open()
        page = document.new_page()
        text = path.read_text(encoding="utf-8", errors="replace")
        page.insert_textbox(fitz.Rect(48, 48, 548, 780), text[:12000])
        document.save(target)
        document.close()
    except Exception:
        return None
    return target


def _hyperrag_span_attrs(phase: str, **extra: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {ATTR_PHASE: phase, **extra}
    source_id = get_source_id()
    if source_id != "unknown":
        attrs[ATTR_RUN_ID] = source_id
    return attrs


@dataclass(frozen=True)
class IndexJobAccepted:
    job_id: str
    library_id: str
    asset_id: str
    status: str = "queued"


@dataclass(frozen=True)
class DeleteIndexResult:
    job_id: str
    library_id: str
    asset_id: str
    deleted: bool = True
    message: str | None = None


@dataclass(frozen=True)
class KnowledgeSearchResultPayload:
    library_id: str
    query: str
    evidence: list[Evidence] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class HyperRAGAdapter:
    """Minimal facade around HyperRAGV2 for viknow2."""

    def __init__(self, config: AppConfig):
        if config.knowledge is None:
            raise RuntimeError("knowledge backend config is required")
        backend = config.knowledge.hyperrag
        self._config_path = Path(backend.config_path)
        self._repo_root = Path(backend.repo_root) if backend.repo_root is not None else None
        self._app: Any | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_thread: threading.Thread | None = None
        self._runtime_lock = threading.Lock()
        self._evidence_metadata_resolver: Callable[..., dict[str, Any]] | None = None
        self._runtime_work_semaphore: asyncio.Semaphore | None = None
        self._runtime_work_semaphore_limit: int | None = None
        self._runtime_concurrency_limit = backend.runtime_concurrency
        self._sync_bridge_concurrency_limit = backend.sync_bridge_concurrency
        # Cap threads blocked in future.result(); avoids pile-up when the loop is busy.
        self._sync_bridge_semaphore = threading.Semaphore(backend.sync_bridge_concurrency)
        self._app_config = config
        self.default_library_id = config.knowledge.default_library_id
        self.default_query_mode = backend.default_query_mode
        self.default_top_k = backend.default_top_k

    def set_evidence_metadata_resolver(
        self,
        resolver: Callable[..., dict[str, Any]],
    ) -> None:
        self._evidence_metadata_resolver = resolver

    def reload_config(self, config: AppConfig) -> None:
        """Apply a freshly loaded config and drop cached HyperRAG runtime state."""

        if config.knowledge is None:
            raise RuntimeError("knowledge backend config is required")
        backend = config.knowledge.hyperrag
        self._app_config = config
        self._config_path = Path(backend.config_path)
        self._repo_root = Path(backend.repo_root) if backend.repo_root is not None else None
        self.default_library_id = config.knowledge.default_library_id
        self.default_query_mode = backend.default_query_mode
        self.default_top_k = backend.default_top_k
        self._runtime_concurrency_limit = backend.runtime_concurrency
        self._sync_bridge_concurrency_limit = backend.sync_bridge_concurrency
        self._sync_bridge_semaphore = threading.Semaphore(backend.sync_bridge_concurrency)
        self._runtime_work_semaphore = None
        self._runtime_work_semaphore_limit = None
        with self._runtime_lock:
            self._app = None

    @property
    def app(self) -> Any:
        return self._run_in_runtime_loop_sync(self._ensure_app)

    async def start_background_runtime(self) -> None:
        """Start HyperRAG workers so persisted jobs resume after an app restart."""

        await self._run_in_runtime_loop_async(lambda: self._ensure_app().initialize())

    def _ensure_app(self) -> Any:
        if self._app is not None:
            return self._app
        with self._runtime_lock:
            if self._app is not None:
                return self._app
            runtime_config_path = materialize_hyperrag_config(self._app_config)
            self._config_path = runtime_config_path
            self._apply_runtime_env(runtime_config_path)
            load_config_from_yaml, hyperrag_app = _load_hyperrag_runtime(
                repo_root=self._repo_root,
                config_path=runtime_config_path,
            )
            hyperrag_config = load_config_from_yaml(str(runtime_config_path))
            self._app = hyperrag_app(
                config=hyperrag_config,
                config_path=str(runtime_config_path),
            )
            self._install_index_tracing()
        return self._app

    def _install_index_tracing(self) -> None:
        try:
            from viknow3.application.hyperrag.index_tracing import (
                configure_index_tracing,
                install_index_tracing_hooks,
                is_index_tracing_enabled,
            )

            langfuse = self._app_config.observability.langfuse
            if langfuse.index_enabled is None:
                # Mirror agent enablement: config.enabled or VIKNOW_LANGFUSE_ENABLED.
                if langfuse.enabled:
                    configure_index_tracing(enabled=True)
                else:
                    configure_index_tracing(enabled=None)
            else:
                configure_index_tracing(enabled=bool(langfuse.index_enabled))
            install_index_tracing_hooks()
            from loguru import logger

            logger.info(
                "[index-langfuse] installed (enabled={})",
                is_index_tracing_enabled(),
            )
        except Exception:
            from loguru import logger

            logger.opt(exception=True).warning(
                "[index-langfuse] failed to install HyperRAG index tracing hooks"
            )

    def _ensure_runtime_loop(self) -> asyncio.AbstractEventLoop:
        if (
            self._runtime_loop is not None
            and self._runtime_thread is not None
            and self._runtime_thread.is_alive()
        ):
            return self._runtime_loop

        with self._runtime_lock:
            if (
                self._runtime_loop is not None
                and self._runtime_thread is not None
                and self._runtime_thread.is_alive()
            ):
                return self._runtime_loop

            ready = threading.Event()
            runtime_loop: asyncio.AbstractEventLoop | None = None

            def runner() -> None:
                nonlocal runtime_loop
                runtime_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(runtime_loop)
                self._runtime_loop = runtime_loop
                ready.set()
                runtime_loop.run_forever()

            thread = threading.Thread(
                target=runner,
                name="hyperrag-adapter-loop",
                daemon=True,
            )
            thread.start()
            ready.wait()
            if runtime_loop is None:
                raise RuntimeError("failed to initialize HyperRAG runtime loop")
            self._runtime_thread = thread
            return runtime_loop

    def _sync_bridge_semaphore_get(self) -> threading.Semaphore:
        sem = self.__dict__.get("_sync_bridge_semaphore")
        if sem is None:
            sem = threading.Semaphore(getattr(self, "_sync_bridge_concurrency_limit", 64))
            self._sync_bridge_semaphore = sem
        return sem

    def shutdown(self) -> None:
        try:
            if self._app is not None:
                self._run_in_runtime_loop_sync(self._app.shutdown)
                self._app = None
        finally:
            self._stop_runtime_loop()

    def _stop_runtime_loop(self) -> None:
        loop = self._runtime_loop
        thread = self._runtime_thread
        if loop is None:
            self._runtime_thread = None
            return

        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=5)

        if thread is None or not thread.is_alive():
            if not loop.is_closed():
                loop.close()
            self._runtime_loop = None
            self._runtime_thread = None

    async def _invoke_runtime(self, operation) -> Any:
        # Run operation() off the event-loop thread so sync I/O (e.g. storage get_all)
        # cannot freeze hyperrag-adapter-loop. Coroutines returned by HyperRAG a* APIs
        # are still awaited on this loop.
        result = await asyncio.to_thread(operation)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _invoke_runtime_in_context(
        self,
        operation,
        *,
        context: contextvars.Context | None,
    ) -> Any:
        sem = getattr(self, "_runtime_work_semaphore", None)
        runtime_limit = getattr(self, "_runtime_concurrency_limit", 64)
        work_limit = getattr(self, "_runtime_work_semaphore_limit", None)
        if sem is None or work_limit != runtime_limit:
            sem = asyncio.Semaphore(runtime_limit)
            self._runtime_work_semaphore = sem
            self._runtime_work_semaphore_limit = runtime_limit
        async with sem:
            coro = self._invoke_runtime(operation)
            if context is not None:
                task = context.run(asyncio.create_task, coro)
                return await task
            return await coro

    async def _run_in_runtime_loop_async(self, operation) -> Any:
        loop = self._ensure_runtime_loop()
        caller_context = contextvars.copy_context()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._run_in_runtime_loop_sync(operation)

        if running_loop is loop:
            return await self._invoke_runtime_in_context(operation, context=caller_context)

        future = asyncio.run_coroutine_threadsafe(
            self._invoke_runtime_in_context(operation, context=caller_context),
            loop,
        )
        return await asyncio.wrap_future(future)

    def _run_in_runtime_loop_sync(self, operation) -> Any:
        if self._runtime_thread is not None and threading.current_thread() is self._runtime_thread:
            result = operation()
            if inspect.isawaitable(result):
                raise RuntimeError(
                    "cannot synchronously wait for a HyperRAG coroutine on the runtime loop"
                )
            return result

        loop = self._ensure_runtime_loop()
        caller_context = contextvars.copy_context()
        with self._sync_bridge_semaphore_get():
            future = asyncio.run_coroutine_threadsafe(
                self._invoke_runtime_in_context(operation, context=caller_context),
                loop,
            )
            return future.result()

    def _apply_runtime_env(self, config_path: Path) -> None:
        raw_config = self._load_yaml(config_path)
        env_config = {
            **self._load_application_env(config_path, raw_config),
            **self._normalize_env(raw_config.get("env")),
        }
        for env_name, env_value in env_config.items():
            if env_value is None:
                continue
            os.environ[env_name] = str(env_value)

    def _load_application_env(
        self, config_path: Path, raw_config: dict[str, Any]
    ) -> dict[str, Any]:
        application_config_path = raw_config.get("application_config_path")
        if not application_config_path:
            return {}
        app_path = Path(str(application_config_path))
        if not app_path.is_absolute():
            app_path = (config_path.parent / app_path).resolve()
        app_config = self._load_yaml(app_path)
        return self._normalize_env((app_config.get("hyperrag") or {}).get("env"))

    @staticmethod
    def _normalize_env(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {str(key): env_value for key, env_value in value.items()}

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file) or {}
        if not isinstance(payload, dict):
            return {}
        return payload

    async def submit_index_async(
        self,
        *,
        library_id: str,
        asset_id: str,
        asset_path: str,
        compile_mode: Literal["pre_compile", "fast_ingest"] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> IndexJobAccepted:
        fast_ingest, pre_compile = _compile_mode_flags(compile_mode)
        job_id = await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aadd_asset(
                workspace=library_id,
                asset_id=asset_id,
                asset_path=asset_path,
                fast_ingest=fast_ingest,
                pre_compile=pre_compile,
                params=params,
                request_id=request_id,
            )
        )
        return IndexJobAccepted(job_id=job_id, library_id=library_id, asset_id=asset_id)

    def submit_index(
        self,
        *,
        library_id: str,
        asset_id: str,
        asset_path: str,
        compile_mode: Literal["pre_compile", "fast_ingest"] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> IndexJobAccepted:
        fast_ingest, pre_compile = _compile_mode_flags(compile_mode)
        job_id = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aadd_asset(
                workspace=library_id,
                asset_id=asset_id,
                asset_path=asset_path,
                fast_ingest=fast_ingest,
                pre_compile=pre_compile,
                params=params,
                request_id=request_id,
            )
        )
        return IndexJobAccepted(job_id=job_id, library_id=library_id, asset_id=asset_id)

    async def delete_index_async(
        self,
        *,
        library_id: str,
        asset_id: str,
        request_id: str | None = None,
    ) -> DeleteIndexResult:
        job_id = await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().adelete_asset(
                workspace=library_id,
                asset_id=asset_id,
                request_id=request_id,
            )
        )
        return DeleteIndexResult(job_id=job_id, library_id=library_id, asset_id=asset_id)

    def delete_index(
        self,
        *,
        library_id: str,
        asset_id: str,
        request_id: str | None = None,
    ) -> DeleteIndexResult:
        job_id = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().adelete_asset(
                workspace=library_id,
                asset_id=asset_id,
                request_id=request_id,
            )
        )
        return DeleteIndexResult(job_id=job_id, library_id=library_id, asset_id=asset_id)

    def resolve_asset_file_path(self, *, library_id: str, asset_id: str) -> str | None:
        """Return the indexed file path for one asset in a library."""
        _, _, asset_status_enum = _load_hyperrag_storage(
            repo_root=self._repo_root,
            config_path=self._config_path,
        )
        self._run_in_runtime_loop_sync(lambda: self._ensure_app()._ensure_core_runtime())
        status_map = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().alist_assets(library_id)
        )
        status = status_map.get(asset_id)
        if status is None or getattr(status, "status", None) == asset_status_enum.DELETED:
            return None
        return getattr(status, "file_path", None)

    def resolve_clip_path(
        self, *, library_id: str, asset_id: str, begin_ts: float, end_ts: float
    ) -> Path | None:
        """Return the clip file path for a video segment, or None if unavailable."""
        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return None
        video_index = self._resolve_video_index(
            working_dir / library_id / "video_cache",
            asset_id,
        )
        if video_index is None:
            return None
        index_path, index = video_index
        for segment in index.get("segments") or []:
            loc = segment.get("location") or {}
            seg_begin = self._to_float(loc.get("begin_ts"))
            seg_end = self._to_float(loc.get("end_ts"))
            if (
                seg_begin is not None
                and seg_end is not None
                and abs(seg_begin - begin_ts) < 1.0
                and abs(seg_end - end_ts) < 1.0
            ):
                clip_dir = index_path.parent / "clips"
                clip_file = clip_dir / f"{segment['segment_id']}.mp4"
                if clip_file.exists():
                    return clip_file
        return None

    def resolve_document_render_path(self, *, library_id: str, asset_id: str) -> Path | None:
        """Return the MinerU-renderable document artifact for one asset."""
        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return None
        mineru_index = self._resolve_mineru_index(
            working_dir / library_id / "mineru_cache",
            asset_id,
        )
        if mineru_index is None:
            return self._resolve_renderable_asset_file(library_id=library_id, asset_id=asset_id)
        index_path, index = mineru_index
        method_dir = index_path.parent
        for pattern in ("*_origin.pdf", "*_layout.pdf"):
            matches = sorted(method_dir.glob(pattern))
            if matches:
                return matches[0]

        asset = index.get("asset") or {}
        if isinstance(asset, dict):
            source_uri = str(asset.get("source_uri") or "")
            if source_uri:
                source_path = Path(source_uri)
                if source_path.exists() and source_path.is_file():
                    return source_path

        top_level_pdf = method_dir.parent.with_suffix(".pdf")
        if top_level_pdf.exists() and top_level_pdf.is_file():
            return top_level_pdf
        return self._resolve_renderable_asset_file(library_id=library_id, asset_id=asset_id)

    def _resolve_renderable_asset_file(self, *, library_id: str, asset_id: str) -> Path | None:
        file_path = self.resolve_asset_file_path(library_id=library_id, asset_id=asset_id)
        if not file_path:
            return None
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return None
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".markdown"}:
            return _text_file_as_single_page_pdf(path)
        if suffix not in {
            ".pdf",
            ".docx",
            ".xlsx",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
            ".gif",
        }:
            return None
        return path

    def resolve_asset_image_path(
        self,
        *,
        library_id: str,
        asset_id: str,
        image_name: str,
    ) -> Path | None:
        """Return one MinerU extracted image path for an asset."""
        image_name = Path(str(image_name or "")).name
        if not image_name:
            return None
        resolved = self._resolve_mineru_image_path(
            library_id=library_id,
            asset_id=asset_id,
            image_ref=image_name,
        )
        if resolved is None:
            return None
        return Path(resolved)

    def _resolve_video_index(
        self, video_cache_dir: Path, asset_id: str
    ) -> tuple[Path, dict[str, Any]] | None:
        direct_index_path = video_cache_dir / asset_id / "index.json"
        if direct_index_path.exists():
            return direct_index_path, self._load_json(direct_index_path)
        if not video_cache_dir.exists() or not video_cache_dir.is_dir():
            return None
        for cache_entry in video_cache_dir.iterdir():
            index_path = cache_entry / "index.json"
            if not index_path.is_file():
                continue
            index = self._load_json(index_path)
            if self._video_index_matches_asset_id(index, asset_id):
                return index_path, index
        return None

    def _resolve_mineru_index(
        self, mineru_cache_dir: Path, asset_id: str
    ) -> tuple[Path, dict[str, Any]] | None:
        direct_root = mineru_cache_dir / asset_id
        if direct_root.exists() and direct_root.is_dir():
            for method_dir in direct_root.iterdir():
                index_path = method_dir / "index.json"
                if not index_path.is_file():
                    continue
                index = self._load_json(index_path)
                if self._mineru_index_matches_asset_id(index, asset_id):
                    return index_path, index
        if not mineru_cache_dir.exists() or not mineru_cache_dir.is_dir():
            return None
        for cache_entry in mineru_cache_dir.iterdir():
            if not cache_entry.is_dir():
                continue
            for method_dir in cache_entry.iterdir():
                index_path = method_dir / "index.json"
                if not index_path.is_file():
                    continue
                index = self._load_json(index_path)
                if self._mineru_index_matches_asset_id(index, asset_id):
                    return index_path, index
        return None

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError(f"expected JSON object in {path}")
        return payload

    @staticmethod
    def _video_index_matches_asset_id(index: dict[str, Any], asset_id: str) -> bool:
        asset = index.get("asset") or {}
        if not isinstance(asset, dict):
            asset = {}
        return asset_id in {
            str(index.get("doc_id") or ""),
            str(asset.get("doc_id") or ""),
        }

    @staticmethod
    def _mineru_index_matches_asset_id(index: dict[str, Any], asset_id: str) -> bool:
        asset = index.get("asset") or {}
        if not isinstance(asset, dict):
            asset = {}
        return asset_id in {
            str(index.get("doc_id") or ""),
            str(asset.get("doc_id") or ""),
            str(asset.get("title") or ""),
        }

    def _resolve_working_dir(self) -> Path | None:
        """Resolve the HyperRAG working_dir from config."""
        raw = self._load_yaml(self._config_path)
        if not str(raw.get("working_dir") or "").strip():
            self._config_path = materialize_hyperrag_config(self._app_config)
            raw = self._load_yaml(self._config_path)
        working_dir_str = str(raw.get("working_dir") or "").strip()
        if not working_dir_str:
            return None
        working_dir = Path(working_dir_str)
        if not working_dir.is_absolute():
            working_dir = (self._config_path.parent / working_dir).resolve()
        return working_dir

    def get_asset_status(self, *, library_id: str, asset_id: str) -> Any | None:
        self._run_in_runtime_loop_sync(lambda: self._ensure_app()._ensure_core_runtime())
        return self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aget_asset_status(
                workspace=library_id,
                asset_id=asset_id,
            )
        )

    def get_asset_chunk_count(self, *, library_id: str, asset_id: str) -> int:
        self._run_in_runtime_loop_sync(lambda: self._ensure_app()._ensure_core_runtime())
        return int(
            self._run_in_runtime_loop_sync(
                lambda: self._ensure_app().aget_chunk_count(
                    workspace=library_id,
                    file_id=asset_id,
                )
            )
        )

    def resolve_video_semantic_path(self, *, library_id: str, asset_id: str) -> Path | None:
        """Resolve the canonical video semantic artifact without starting a runtime."""
        root = self._resolve_working_dir()
        if root is None:
            return None
        for identity in (library_id, asset_id):
            if not identity or identity in {".", ".."} or Path(identity).name != identity:
                raise ValueError("invalid library or asset identity")
        path = (root / library_id / "video_cache" / asset_id / "sop-full.json").resolve()
        if root.resolve() not in path.parents:
            raise ValueError("semantic path outside knowledge root")
        return path if path.is_file() else None

    async def get_video_semantic_async(self, *, library_id: str, asset_id: str):
        """Read complete semantic evidence for consumers outside the indexing pipeline."""
        return await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aget_video_semantic(workspace=library_id, asset_id=asset_id)
        )

    async def get_video_global_summary_async(self, *, library_id: str, asset_id: str) -> str:
        return (await self.get_video_summary_async(library_id=library_id, asset_id=asset_id))[
            "global_summary"
        ]

    async def get_video_summary_async(self, *, library_id: str, asset_id: str) -> dict[str, Any]:
        from actio.global_context import normalize_global_context

        payload = await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aget_video_semantic(
                workspace=library_id,
                asset_id=asset_id,
            )
        )
        if not isinstance(payload, dict):
            raise ValueError("视频索引语义文件不存在")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("视频索引语义文件缺少 metadata")
        pending = metadata.get("global_summary_submission")
        if isinstance(pending, dict) and pending.get("status") == "unknown":
            raise ValueError(f"请先核查未确认的索引任务 {pending.get('job_id', '')}，不要重复提交")
        return {
            "global_summary": str(metadata.get("global_summary") or "").strip(),
            "global_context": normalize_global_context(metadata.get("global_context")),
        }

    async def update_video_global_summary_async(
        self,
        *,
        library_id: str,
        asset_id: str,
        global_summary: str,
        global_context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str:
        job_id = await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aupdate_video_global_summary(
                workspace=library_id,
                asset_id=asset_id,
                global_summary=global_summary,
                global_context=global_context,
                request_id=request_id,
            )
        )
        return str(job_id)

    def list_asset_segments(self, *, library_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Return all indexed chunks for an asset, sorted by order."""
        app = self._run_in_runtime_loop_sync(self._ensure_app)
        chunks = self._run_in_runtime_loop_sync(
            lambda: self._list_asset_segments_by_identity(
                app,
                library_id=library_id,
                asset_id=asset_id,
            )
        )
        if chunks:
            return self._attach_indexed_asr_candidates(
                library_id=library_id,
                asset_id=asset_id,
                chunks=chunks,
            )
        chunks = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aget_asset_chunks(
                workspace=library_id,
                file_id=asset_id,
            )
        )
        if chunks:
            return self._attach_indexed_asr_candidates(
                library_id=library_id,
                asset_id=asset_id,
                chunks=chunks,
            )
        chunks = self._list_cached_video_segments(library_id=library_id, asset_id=asset_id)
        # An empty retrieval alone cannot establish that this asset exists. In
        # particular, a display filename is not necessarily the indexed asset ID.
        if not chunks and self.get_asset_status(library_id=library_id, asset_id=asset_id) is None:
            raise ValueError(
                "source_not_resolved: no indexed segments or asset metadata were found "
                "for this library_id/asset_id pair. Reuse canonical IDs from existing "
                "knowledge_discover/knowledge_search results, or resolve the source first. "
                "This does not establish that the intended document/video is unindexed."
            )
        return self._attach_indexed_asr_candidates(
            library_id=library_id,
            asset_id=asset_id,
            chunks=chunks,
        )

    def _attach_indexed_asr_candidates(
        self,
        *,
        library_id: str,
        asset_id: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expose indexed Actio ASR as review-only candidates on overlapping video chunks."""
        candidates = self._indexed_actio_asr_candidates(
            library_id=library_id,
            asset_id=asset_id,
        )
        if not candidates:
            return chunks
        enriched: list[dict[str, Any]] = []
        for chunk in chunks:
            metadata = chunk.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            begin_ts = self._to_float(metadata.get("begin_ts"))
            end_ts = self._to_float(metadata.get("end_ts"))
            if begin_ts is not None and end_ts is not None:
                overlapping = _overlapping_indexed_candidates(
                    candidates,
                    start_seconds=begin_ts,
                    end_seconds=end_ts,
                )
                if overlapping:
                    metadata["candidate_evidence"] = overlapping
            enriched.append({**chunk, "metadata": metadata})
        return enriched

    def _indexed_actio_asr_candidates(
        self,
        *,
        library_id: str,
        asset_id: str,
    ) -> list[dict[str, Any]]:
        """Read a persisted Actio ASR artifact without treating it as verified evidence."""
        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return []
        artifact = working_dir / library_id / "video_cache" / asset_id / "actio_work" / "asr.json"
        if not artifact.is_file():
            return []
        try:
            payload = self._load_json(artifact)
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            return []
        candidates: list[dict[str, Any]] = []
        for node in raw_nodes:
            if not isinstance(node, dict):
                continue
            start_seconds = self._to_float(node.get("start"))
            end_seconds = self._to_float(node.get("end"))
            asr = node.get("asr")
            text = str(asr.get("text") or "").strip() if isinstance(asr, dict) else ""
            if (
                start_seconds is None
                or end_seconds is None
                or end_seconds < start_seconds
                or not text
            ):
                continue
            candidates.append(
                {
                    "candidate_id": (
                        f"actio-asr-{round(start_seconds * 1000)}-{round(end_seconds * 1000)}"
                    ),
                    "kind": "asr",
                    "text": text,
                    "time_range": {
                        "start_seconds": start_seconds,
                        "end_seconds": end_seconds,
                    },
                    "source": "indexed_actio_asr",
                    "review_status": "unreviewed",
                }
            )
        return candidates

    def _list_cached_video_segments(
        self,
        *,
        library_id: str,
        asset_id: str,
    ) -> list[dict[str, Any]]:
        """Recover video segments when runtime asset UUIDs differ from cache doc IDs."""
        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return []

        candidate_ids = [asset_id]
        source_path = self.resolve_asset_file_path(library_id=library_id, asset_id=asset_id)
        try:
            status = self.get_asset_status(library_id=library_id, asset_id=asset_id)
        except Exception:
            status = None
        raw_aliases = [source_path or ""]
        file_data = (
            status.get("file_data")
            if isinstance(status, dict)
            else getattr(status, "file_data", None)
        )
        if isinstance(file_data, dict):
            raw_aliases.append(str(file_data.get("title") or ""))

        semantic_chunks = self._list_sop_semantic_cache_segments(
            library_id=library_id,
            asset_id=asset_id,
            source_path=source_path,
        )
        if semantic_chunks:
            return semantic_chunks

        for raw_alias in raw_aliases:
            for candidate_id in self._video_cache_id_candidates(raw_alias):
                if candidate_id not in candidate_ids:
                    candidate_ids.append(candidate_id)

        video_cache_dir = working_dir / library_id / "video_cache"
        video_index = next(
            (
                resolved
                for candidate_id in candidate_ids
                if (resolved := self._resolve_video_index(video_cache_dir, candidate_id))
                is not None
            ),
            None,
        )
        if video_index is None:
            legacy_root = Path(
                os.getenv("VIKNOW_LEGACY_KBINDEX_ROOT", "/data/test/app_data/KBIndex")
            )
            if legacy_root.is_dir():
                for kb_dir in legacy_root.iterdir():
                    if not kb_dir.is_dir():
                        continue
                    for candidate_id in candidate_ids:
                        index_path = kb_dir / "video_cache" / candidate_id / "index.json"
                        if index_path.is_file():
                            video_index = (index_path, self._load_json(index_path))
                            break
                    if video_index is not None:
                        break
        if video_index is None:
            return []

        index_path, index = video_index
        index_asset = index.get("asset") or {}
        if not isinstance(index_asset, dict):
            index_asset = {}
        doc_id = str(index.get("doc_id") or index_asset.get("doc_id") or index_path.parent.name)
        source_uri = str(index_asset.get("source_uri") or source_path or "")
        title = str(index_asset.get("title") or doc_id)
        chunks: list[dict[str, Any]] = []
        for segment in index.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("segment_id") or "").strip()
            if not segment_id:
                continue
            location = segment.get("location") or {}
            if not isinstance(location, dict):
                location = {}
            begin_ts = self._to_float(location.get("begin_ts"))
            end_ts = self._to_float(location.get("end_ts"))
            clip_path = index_path.parent / "clips" / f"{segment_id}.mp4"
            metadata = {
                **(segment.get("extra") or {}),
                "asset_id": asset_id,
                "video_id": doc_id,
                "source_video_id": doc_id,
                "doc_id": doc_id,
                "title": title,
                "chunk_type": "video",
                "begin_ts": begin_ts,
                "end_ts": end_ts,
                "clip_path": str(clip_path) if clip_path.is_file() else "",
                "source_uri": source_uri,
            }
            chunks.append(
                {
                    "chunk_id": segment_id,
                    "full_doc_id": doc_id,
                    "file_path": source_uri,
                    "content": str(segment.get("text") or ""),
                    "order": int(segment.get("order") or 0),
                    "metadata": metadata,
                }
            )
        chunks.sort(key=lambda item: (int(item.get("order") or 0), str(item["chunk_id"])))
        return chunks

    def _list_sop_semantic_cache_segments(
        self,
        *,
        library_id: str,
        asset_id: str,
        source_path: str | None,
    ) -> list[dict[str, Any]]:
        """Project canonical top-level video segments into adapter chunks."""
        from hyperrag.utils.segment_tools import format_caption_data_text

        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return []
        semantic_path = working_dir / library_id / "video_cache" / asset_id / "sop-full.json"
        if not semantic_path.is_file():
            return []
        payload = self._load_json(semantic_path)
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        title = str(metadata.get("title") or asset_id)
        source_id = _semantic_video_source_id(metadata) or asset_id
        chunks: list[dict[str, Any]] = []
        for order, segment in enumerate(payload.get("segments") or []):
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("segment_id") or "").strip()
            time_range = segment.get("time_range")
            if not segment_id or not isinstance(time_range, list) or len(time_range) != 2:
                continue
            begin_ts = self._to_float(time_range[0])
            end_ts = self._to_float(time_range[1])
            if begin_ts is None or end_ts is None or end_ts < begin_ts:
                continue
            data = segment.get("data")
            data = data if isinstance(data, dict) else {}
            content = format_caption_data_text(data)
            clip_path = ""
            caption_metadata = data.get("caption_metadata")
            if isinstance(caption_metadata, dict):
                clip_path = str(caption_metadata.get("clip_path") or "")
            video_id = str(segment.get("video_id") or source_id or asset_id)
            chunks.append(
                {
                    "chunk_id": segment_id,
                    "full_doc_id": asset_id,
                    "file_path": source_path or "",
                    "content": content,
                    "order": order,
                    "metadata": {
                        "asset_id": asset_id,
                        "video_id": video_id,
                        "source_video_id": video_id,
                        "doc_id": asset_id,
                        "title": title,
                        "chunk_type": "video",
                        "begin_ts": begin_ts,
                        "end_ts": end_ts,
                        "clip_path": clip_path,
                        "source_uri": source_path or "",
                        "segment_id": segment_id,
                    },
                }
            )
        return chunks

    @staticmethod
    def _video_cache_id_candidates(raw_value: str) -> list[str]:
        stem = Path(str(raw_value or "")).stem.strip()
        if not stem:
            return []
        candidates = [stem]
        normalized = re.sub(r"(?:\.normalized|[（(]\d+[）)])$", "", stem).strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
        return candidates

    async def _list_asset_segments_by_identity(
        self,
        app: Any,
        *,
        library_id: str,
        asset_id: str,
    ) -> list[dict[str, Any]]:
        experiment = await app._get_kg_experiment(library_id)
        vector_store = experiment._get_vector_store()
        await vector_store.ensure_initialized()
        table = f"{vector_store.schema}.{vector_store.chunk_table}"
        rows = await vector_store.db.query(
            f"""
            SELECT item_id, file_path, content, metadata
            FROM {table}
            WHERE workspace = $1
              AND (
                metadata->>'asset_id' = $2
                OR metadata->>'video_id' = $2
                OR metadata->>'source_id' = $2
              )
            """,
            [library_id, asset_id],
            multirows=True,
        )
        if not rows:
            return []
        decode_row = getattr(vector_store, "_decode_row", None)
        decoded_rows = [decode_row(row) for row in rows] if callable(decode_row) else rows
        status_map = await app._get_workspace_asset_status_cache(library_id)
        status_by_file_path = app._build_status_by_file_path(status_map)
        chunks = [
            await app._normalize_kg_chunk_row(
                library_id,
                row,
                status_map=status_map,
                status_by_file_path=status_by_file_path,
            )
            for row in decoded_rows
        ]
        chunks.sort(key=lambda item: (int(item.get("order") or 0), str(item.get("chunk_id") or "")))
        return chunks

    def get_chunk_by_id(self, *, library_id: str, evidence_id: str) -> dict[str, Any] | None:
        """Resolve an indexed chunk and identity-matched original table sources."""

        async def resolve():
            app = self._ensure_app()
            chunk = await app.aget_chunk_by_id(
                workspace=library_id,
                chunk_id=evidence_id,
            )
            if chunk is None:
                return None
            metadata = chunk.get("metadata") or {}
            asset_id = metadata.get("asset_id") or chunk.get("full_doc_id")
            if (
                not asset_id
                or metadata.get("edited")
                or metadata.get("chunk_type") != "image"
                or not metadata.get("locations")
            ):
                return chunk
            snapshot = await app.aget_file_snapshot(library_id, asset_id)
            return bind_table_sources(
                chunk,
                snapshot,
                snapshot_directory=(
                    app.artifact_store.get_mineru_cache_root(library_id) / asset_id / "vlm"
                ),
            )

        return self._run_in_runtime_loop_sync(resolve)

    async def search_async(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> KnowledgeSearchResultPayload:
        async with start_span(
            "hyperrag.search",
            **_hyperrag_span_attrs("hyperrag.search", **{"viknow.library_id": library_id}),
        ):
            query_kwargs: dict[str, Any] = {
                "workspace": library_id,
                "query": query,
                "retrieval_mode": self.default_query_mode,
                "chunk_top_k": top_k if top_k is not None else None,
            }
            if threshold is not None:
                query_kwargs["rerank_score_threshold"] = threshold
            result = await self._run_in_runtime_loop_async(
                lambda: self._ensure_app().aquery_data(**query_kwargs)
            )
        return await asyncio.to_thread(
            self._build_search_payload,
            library_id=library_id,
            query=query,
            result=result,
            top_k=top_k,
        )

    async def search_by_image_async(
        self,
        *,
        library_id: str,
        image_data_url: str,
        query: str = "",
        mix_search: bool = True,
        top_k: int | None = None,
    ) -> KnowledgeSearchResultPayload:
        result = await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aquery_data_by_image(
                workspace=library_id,
                image_url=image_data_url,
                query_text=query,
                mix_search=mix_search,
                chunk_top_k=top_k if top_k is not None else None,
            )
        )
        normalized_query = query if (mix_search and str(query or "").strip()) else ""
        return await asyncio.to_thread(
            self._build_search_payload,
            library_id=library_id,
            query=normalized_query,
            result=result,
        )

    async def discover_libraries_async(
        self,
        query: str,
        *,
        top_k: int | None = None,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]:
        """Discover libraries from the bounded HyperRAG library and asset catalog."""
        asset_limit = self._discover_asset_matches_per_library() if query.strip() else None
        normalized_ids = list(dict.fromkeys(library_ids)) if library_ids is not None else None
        async with start_span(
            "hyperrag.discover",
            **_hyperrag_span_attrs("hyperrag.discover"),
        ):
            payload = await self._run_in_runtime_loop_async(
                lambda: self._ensure_app().adiscover_library_matches(
                    query,
                    library_ids=normalized_ids,
                    top_k=top_k,
                    asset_limit=asset_limit,
                )
            )
        return self._build_catalog_discovery_results(payload)

    def discover_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]:
        """Synchronous bounded catalog discovery for the Hermes compatibility adapter."""
        asset_limit = self._discover_asset_matches_per_library() if query.strip() else None
        normalized_ids = list(dict.fromkeys(library_ids)) if library_ids is not None else None
        with start_span_sync("hyperrag.discover", **_hyperrag_span_attrs("hyperrag.discover")):
            payload = self._run_in_runtime_loop_sync(
                lambda: self._ensure_app().adiscover_library_matches(
                    query,
                    library_ids=normalized_ids,
                    top_k=top_k,
                    asset_limit=asset_limit,
                )
            )
        return self._build_catalog_discovery_results(payload)

    def _discover_asset_matches_per_library(self) -> int:
        raw = self._load_yaml(self._config_path)
        return max(1, int(raw.get("knowledge_discover_asset_matches_per_library", 3) or 3))

    @staticmethod
    def _build_catalog_discovery_results(payload: Any) -> list[KnowledgeLibrarySearchResult]:
        if not isinstance(payload, dict):
            raise RuntimeError("knowledge discovery catalog returned an invalid payload")
        rows = payload.get("library_matches")
        if not isinstance(rows, list):
            raise RuntimeError("knowledge discovery catalog response is missing library_matches")

        results: list[KnowledgeLibrarySearchResult] = []
        total = max(1, len(rows))
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("library"), dict):
                continue
            library_row = row["library"]
            library_id = str(library_row.get("library_id") or "").strip()
            if not library_id:
                continue
            asset_matches: list[KnowledgeAssetDiscovery] = []
            for match in row.get("asset_matches") or []:
                if not isinstance(match, dict):
                    continue
                asset_id = str(match.get("asset_id") or "").strip()
                if not asset_id:
                    continue
                asset_matches.append(
                    KnowledgeAssetDiscovery(
                        asset_id=asset_id,
                        title=str(match.get("title") or asset_id),
                        modality="video" if str(match.get("type") or "") == "视频" else "document",
                        summary=str(match.get("summary") or ""),
                        score=None,
                        match_reasons=["title_or_lexical_catalog_match"],
                    )
                )
            results.append(
                KnowledgeLibrarySearchResult(
                    library=KnowledgeLibrary(
                        id=library_id,
                        name=str(library_row.get("title") or library_id),
                        description=str(library_row.get("overview") or ""),
                        metadata={
                            "source": "hyperrag_discovery_catalog",
                            "asset_count": int(library_row.get("asset_count") or 0),
                            "document_count": int(library_row.get("document_count") or 0),
                            "video_count": int(library_row.get("video_count") or 0),
                            "updated_at": library_row.get("updated_at"),
                        },
                    ),
                    score=1.0 - (index / total),
                    reason=(
                        "indexed asset title/summary match"
                        if asset_matches
                        else "knowledge library overview match"
                    ),
                    asset_matches=asset_matches,
                    metadata={"catalog_query": str(payload.get("query") or "")},
                )
            )
        return results

    async def query_kg_async(
        self,
        *,
        library_id: str,
        query: str,
        pre_compile: bool,
        top_k: int,
        threshold: float,
    ) -> dict[str, list[dict[str, str]]]:
        return await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aquery_kg(
                query=query,
                library_id=library_id,
                pre_compile=pre_compile,
                top_k=top_k,
                threshold=threshold,
            )
        )

    async def export_knowledge_package_async(
        self,
        *,
        workspaces: list[str] | None = None,
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aexport_knowledge_package(
                workspaces=workspaces,
                company_replacements=company_replacements,
                alias_fullname_map=alias_fullname_map,
            )
        )

    async def import_knowledge_package_async(
        self,
        *,
        package: dict[str, Any],
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._run_in_runtime_loop_async(
            lambda: self._ensure_app().aimport_knowledge_package(
                package=package,
                company_replacements=company_replacements,
                alias_fullname_map=alias_fullname_map,
            )
        )

    def query_kg(
        self,
        *,
        library_id: str,
        query: str,
        pre_compile: bool,
        top_k: int,
        threshold: float,
    ) -> dict[str, list[dict[str, str]]]:
        return self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aquery_kg(
                query=query,
                library_id=library_id,
                pre_compile=pre_compile,
                top_k=top_k,
                threshold=threshold,
            )
        )

    def export_knowledge_package(
        self,
        *,
        workspaces: list[str] | None = None,
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aexport_knowledge_package(
                workspaces=workspaces,
                company_replacements=company_replacements,
                alias_fullname_map=alias_fullname_map,
            )
        )

    def import_knowledge_package(
        self,
        *,
        package: dict[str, Any],
        company_replacements: dict[str, str] | None = None,
        alias_fullname_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aimport_knowledge_package(
                package=package,
                company_replacements=company_replacements,
                alias_fullname_map=alias_fullname_map,
            )
        )

    def search(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> KnowledgeSearchResultPayload:
        with start_span_sync(
            "hyperrag.search",
            **_hyperrag_span_attrs("hyperrag.search", **{"viknow.library_id": library_id}),
        ):
            return self._search_sync(
                library_id=library_id,
                query=query,
                top_k=top_k,
                threshold=threshold,
            )

    def _search_sync(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> KnowledgeSearchResultPayload:
        query_kwargs: dict[str, Any] = {
            "workspace": library_id,
            "query": query,
            "retrieval_mode": self.default_query_mode,
            "chunk_top_k": top_k if top_k is not None else None,
        }
        if threshold is not None:
            query_kwargs["rerank_score_threshold"] = threshold
        result = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aquery_data(**query_kwargs)
        )
        return self._build_search_payload(
            library_id=library_id,
            query=query,
            result=result,
            top_k=top_k,
        )

    def search_by_image(
        self,
        *,
        library_id: str,
        image_data_url: str,
        query: str = "",
        mix_search: bool = True,
        top_k: int | None = None,
    ) -> KnowledgeSearchResultPayload:
        result = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().aquery_data_by_image(
                workspace=library_id,
                image_url=image_data_url,
                query_text=query,
                mix_search=mix_search,
                chunk_top_k=top_k if top_k is not None else None,
            )
        )
        normalized_query = query if (mix_search and str(query or "").strip()) else ""
        return self._build_search_payload(
            library_id=library_id,
            query=normalized_query,
            result=result,
        )

    def list_libraries(
        self, *, library_ids: Collection[str] | None = None
    ) -> list[KnowledgeLibrary]:
        records = self._list_knowledgebase_records(library_ids=library_ids)
        return [self._build_library(record) for record in records]

    async def list_libraries_async(
        self, *, library_ids: Collection[str] | None = None
    ) -> list[KnowledgeLibrary]:
        if library_ids is not None and not library_ids:
            return []
        await self._run_in_runtime_loop_async(lambda: self._ensure_app()._ensure_core_runtime())
        _, get_knowledgebase_info_storage, _ = _load_hyperrag_storage(
            repo_root=self._repo_root,
            config_path=self._config_path,
        )
        storage = get_knowledgebase_info_storage()
        if library_ids is None:
            records = await self._run_in_runtime_loop_async(storage.get_all)
        else:
            ids = list(dict.fromkeys(library_ids))
            rows = await self._run_in_runtime_loop_async(lambda: storage.get_by_ids(ids))
            records = [storage._to_record(row) for row in rows]
        return [self._build_library(record) for record in records]

    async def search_libraries_async(
        self,
        query: str,
        *,
        top_k: int | None = None,
        score_threshold: float = 0.5,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]:
        async with start_span(
            "hyperrag.discover",
            **_hyperrag_span_attrs("hyperrag.discover"),
        ):
            libraries = await self.list_libraries_async(library_ids=library_ids)
            return await self._rank_libraries_async(
                query,
                libraries,
                top_k=top_k,
                score_threshold=score_threshold,
            )

    async def _rank_libraries_async(
        self,
        query: str,
        libraries: list[KnowledgeLibrary],
        *,
        top_k: int | None = None,
        score_threshold: float = 0.5,
    ) -> list[KnowledgeLibrarySearchResult]:
        normalized_query = normalize_text(query)
        if not normalized_query:
            results = [
                KnowledgeLibrarySearchResult(library=library, score=1.0) for library in libraries
            ]
            return results if top_k is None else results[:top_k]

        normalized_threshold = max(0.0, float(score_threshold))
        rerank_scores = await self._rerank_library_scores_async(normalized_query, libraries)
        return self._build_library_search_results(
            libraries,
            rerank_scores=rerank_scores,
            score_threshold=normalized_threshold,
            top_k=top_k,
        )

    async def _rerank_library_scores_async(
        self,
        query: str,
        libraries: list[KnowledgeLibrary],
    ) -> dict[str, float]:
        if not libraries:
            return {}
        async with start_span("hyperrag.rerank", **_hyperrag_span_attrs("hyperrag.rerank")):
            return await self._run_in_runtime_loop_async(
                lambda: self._rerank_library_scores_body(query, libraries)
            )

    def search_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        score_threshold: float = 0.5,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]:
        with start_span_sync("hyperrag.discover", **_hyperrag_span_attrs("hyperrag.discover")):
            return self._search_libraries(
                query,
                top_k=top_k,
                score_threshold=score_threshold,
                library_ids=library_ids,
            )

    def _search_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        score_threshold: float = 0.5,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]:
        libraries = (
            self.list_libraries()
            if library_ids is None
            else self.list_libraries(library_ids=library_ids)
        )
        normalized_query = normalize_text(query)
        if not normalized_query:
            results = [
                KnowledgeLibrarySearchResult(library=library, score=1.0) for library in libraries
            ]
            return results if top_k is None else results[:top_k]

        normalized_threshold = max(0.0, float(score_threshold))
        rerank_scores = self._rerank_library_scores(normalized_query, libraries)
        return self._build_library_search_results(
            libraries,
            rerank_scores=rerank_scores,
            score_threshold=normalized_threshold,
            top_k=top_k,
        )

    def _build_library_search_results(
        self,
        libraries: list[KnowledgeLibrary],
        *,
        rerank_scores: dict[str, float],
        score_threshold: float,
        top_k: int | None,
    ) -> list[KnowledgeLibrarySearchResult]:
        results: list[KnowledgeLibrarySearchResult] = []
        for library in libraries:
            score = rerank_scores.get(library.id, 0.0)
            if score < score_threshold:
                continue
            results.append(
                KnowledgeLibrarySearchResult(
                    library=library,
                    score=score,
                    reason=(
                        f"rerank summary relevance score={score:.3f} "
                        f"(threshold={score_threshold:.3f})"
                    ),
                )
            )

        results.sort(key=lambda item: item.score or 0.0, reverse=True)
        return results if top_k is None else results[:top_k]

    def _rerank_library_scores(
        self,
        query: str,
        libraries: list[KnowledgeLibrary],
    ) -> dict[str, float]:
        if not libraries:
            return {}
        with start_span_sync("hyperrag.rerank", **_hyperrag_span_attrs("hyperrag.rerank")):
            return self._rerank_library_scores_body(query, libraries)

    def _rerank_library_scores_body(
        self,
        query: str,
        libraries: list[KnowledgeLibrary],
    ) -> dict[str, float]:
        if not libraries:
            return {}
        rerank_config = self._resolve_rerank_config()
        reranker_cls, candidate_cls = _load_hyperrag_reranker(
            repo_root=self._repo_root,
            config_path=self._config_path,
        )
        reranker = reranker_cls(
            api_key=rerank_config["api_key"],
            model=rerank_config["model"],
            api_url=rerank_config["api_url"],
            mode=rerank_config["mode"],
            batch_size=rerank_config["batch_size"],
            batch_concurrency=rerank_config["batch_concurrency"],
            max_retries=rerank_config["max_retries"],
            timeout_seconds=rerank_config["timeout_seconds"],
        )
        candidates = [
            candidate_cls(
                id=library.id,
                text=self._library_rerank_text(library),
                metadata={"library_id": library.id, "library_name": library.name},
            )
            for library in libraries
        ]
        rerank_results = self._run_in_runtime_loop_sync(lambda: reranker.rerank(query, candidates))
        scored: dict[str, float] = {}
        for item in rerank_results:
            candidate_id = str(getattr(item, "candidate_id", "")).strip()
            relevance_score = self._to_float(getattr(item, "relevance_score", None))
            if not candidate_id or relevance_score is None:
                continue
            scored[candidate_id] = relevance_score
        return scored

    def _resolve_rerank_config(self) -> dict[str, Any]:
        raw = self._load_yaml(self._config_path)
        rerank_section = raw.get("rerank")
        rerank = rerank_section if isinstance(rerank_section, dict) else {}

        enabled = rerank.get("enabled")
        if enabled is None:
            enabled = raw.get("rerank_enabled")
        if enabled is False:
            raise RuntimeError("knowledge library rerank is disabled in HyperRAG config")

        model = str(raw.get("rerank_model") or rerank.get("model") or "").strip()
        api_url = str(
            raw.get("rerank_api_url")
            or rerank.get("api_url")
            or raw.get("rerank_base_url")
            or rerank.get("base_url")
            or ""
        ).strip()
        if not model or not api_url:
            raise RuntimeError("knowledge library rerank requires rerank_model and rerank_base_url")
        api_url = self._build_rerank_api_url(api_url)

        api_key = raw.get("rerank_api_key")
        if api_key is None:
            api_key = rerank.get("api_key")
        mode = str(raw.get("rerank_mode") or rerank.get("mode") or "text").strip() or "text"
        batch_size = int(raw.get("rerank_batch_size") or rerank.get("batch_size") or 4)
        batch_concurrency = int(
            raw.get("rerank_batch_concurrency") or rerank.get("batch_concurrency") or 4
        )
        max_retries = int(raw.get("rerank_max_retries") or rerank.get("max_retries") or 2)
        timeout_seconds = float(
            raw.get("rerank_timeout_seconds") or rerank.get("timeout_seconds") or 15.0
        )
        return {
            "api_key": api_key,
            "model": model,
            "api_url": api_url,
            "mode": mode,
            "batch_size": max(1, batch_size),
            "batch_concurrency": max(1, batch_concurrency),
            "max_retries": max(0, max_retries),
            "timeout_seconds": max(1.0, timeout_seconds),
        }

    @staticmethod
    def _build_rerank_api_url(base_url: str) -> str:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            raise RuntimeError("knowledge library rerank requires a non-empty rerank api url")
        if normalized.endswith("/rerank"):
            return normalized
        return f"{normalized}/rerank"

    @staticmethod
    def _library_rerank_text(library: KnowledgeLibrary) -> str:
        return "\n".join(
            part
            for part in (
                f"知识库名称：{normalize_text(library.name)}",
                f"知识库ID：{normalize_text(library.id)}",
                f"知识库摘要：{normalize_text(library.description)}",
            )
            if part.strip()
        )

    def _build_search_payload(
        self,
        *,
        library_id: str,
        query: str,
        result: dict[str, Any],
        chunks: list[dict[str, Any]] | None = None,
        top_k: int | None = None,
    ) -> KnowledgeSearchResultPayload:
        chunks = chunks if chunks is not None else self._result_chunks(result)
        # HyperRAG expands and reranks beyond chunk_top_k. Its final ranking is
        # authoritative, while ViKnow top_k limits the returned evidence window.
        evidence: list[Evidence] = []
        for chunk in chunks:
            built = self.evidence_for_chunk(library_id=library_id, chunk=chunk)
            if built is None:
                continue
            evidence.append(built)
            if top_k is not None and len(evidence) >= top_k:
                break
        return KnowledgeSearchResultPayload(
            library_id=library_id,
            query=query,
            evidence=evidence,
            metadata={
                "retrieval_mode": self.default_query_mode,
                "result_count": len(evidence),
            },
        )

    @staticmethod
    def _result_chunks(result: dict[str, Any]) -> list[dict[str, Any]]:
        return list(result.get("data", {}).get("chunks", []) or [])

    def evidence_for_chunk(
        self,
        *,
        library_id: str,
        chunk: dict[str, Any],
    ) -> Evidence | None:
        metadata = chunk.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        asset_id = str(metadata.get("asset_id") or chunk.get("full_doc_id") or "").strip()
        metadata_resolver = getattr(self, "_evidence_metadata_resolver", None)
        resolved_metadata = (
            metadata_resolver(library_id=library_id, asset_id=asset_id)
            if metadata_resolver is not None and asset_id
            else {}
        )
        metadata = {**metadata, **resolved_metadata}

        chunk_id = str(
            chunk.get("chunk_id") or chunk.get("id") or metadata.get("chunk_id") or ""
        ).strip()
        if not chunk_id:
            return None

        content = str(chunk.get("content") or "")
        title = str(metadata.get("title") or chunk.get("full_doc_id") or chunk_id)
        file_path = str(chunk.get("file_path") or metadata.get("file_path") or "")
        score = next(
            (
                parsed
                for value in (
                    metadata.get("rerank_score"),
                    chunk.get("score"),
                    metadata.get("similarity"),
                    metadata.get("score"),
                    chunk.get("similarity"),
                    chunk.get("bm25_score"),
                    metadata.get("bm25_score"),
                )
                if (parsed := self._to_float(value)) is not None
            ),
            None,
        )
        chunk_type = str(metadata.get("chunk_type") or "").lower()
        begin_ts = self._to_float(metadata.get("begin_ts"))
        end_ts = self._to_float(metadata.get("end_ts"))
        is_video = chunk_type == "video" or begin_ts is not None or end_ts is not None

        evidence_metadata = {
            **metadata,
            "library_id": library_id,
            "full_doc_id": str(chunk.get("full_doc_id") or ""),
            "chunk_id": chunk_id,
            "file_path": file_path,
        }
        if not is_video and evidence_metadata.get("page") is None and evidence_metadata.get("page_number") is None:
            evidence_metadata["page"] = 0

        parts = [
            EvidencePart(kind=EvidencePartKind.TEXT, text=content, metadata={"chunk_id": chunk_id})
        ]
        if is_video and asset_id and begin_ts is not None and end_ts is not None:
            candidate_evidence = _overlapping_indexed_candidates(
                self._indexed_actio_asr_candidates(
                    library_id=library_id,
                    asset_id=asset_id,
                ),
                start_seconds=begin_ts,
                end_seconds=end_ts,
            )
            if candidate_evidence:
                evidence_metadata["candidate_evidence"] = candidate_evidence
        source_refs = self._resolved_source_refs(
            library_id=library_id,
            asset_id=asset_id,
            refs=metadata.get("source_refs"),
        )
        if source_refs:
            evidence_metadata["source_refs"] = source_refs
        image_uri = self._resolve_image_uri(
            library_id=library_id,
            asset_id=asset_id,
            metadata=metadata,
            source_refs=source_refs,
        )
        if chunk_type == "image":
            parts.append(
                EvidencePart(
                    kind=EvidencePartKind.IMAGE,
                    uri=image_uri or None,
                    mime_type=self._guess_image_mime_type(image_uri),
                    metadata={
                        "asset_id": asset_id or metadata.get("asset_id"),
                        "segment_ids": metadata.get("segment_ids") or [],
                    },
                )
            )
            if file_path and file_path != image_uri:
                parts.append(
                    EvidencePart(
                        kind=EvidencePartKind.FILE,
                        uri=file_path,
                        metadata={"asset_id": metadata.get("asset_id")},
                    )
                )
        elif is_video:
            video_uri = str(
                metadata.get("clip_path") or metadata.get("video_clip_path") or file_path
            )
            parts.append(
                EvidencePart(
                    kind=EvidencePartKind.VIDEO,
                    uri=video_uri or None,
                    mime_type="video/mp4",
                    time_range=(
                        EvidenceTimeRange(
                            start_seconds=begin_ts or 0.0,
                            end_seconds=end_ts if end_ts is not None else (begin_ts or 0.0),
                        )
                        if begin_ts is not None or end_ts is not None
                        else None
                    ),
                    metadata={
                        "asset_id": metadata.get("asset_id"),
                        "video_id": metadata.get("video_id"),
                        "source_video_id": metadata.get("source_video_id"),
                        "segment_ids": metadata.get("segment_ids") or [],
                    },
                )
            )
        elif file_path:
            parts.append(
                EvidencePart(
                    kind=EvidencePartKind.FILE,
                    uri=file_path,
                    metadata={"asset_id": asset_id or metadata.get("asset_id")},
                )
            )

        return Evidence(
            id=chunk_id,
            source="knowledge_search",
            title=title,
            score=score,
            parts=parts,
            metadata=evidence_metadata,
        )

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _guess_image_mime_type(image_uri: str) -> str | None:
        suffix = Path(image_uri).suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }.get(suffix)

    def _resolve_image_uri(
        self,
        *,
        library_id: str,
        asset_id: str,
        metadata: dict[str, Any],
        source_refs: list[dict[str, Any]],
    ) -> str:
        raw = str(
            metadata.get("image_url")
            or metadata.get("image_path")
            or metadata.get("image_ref")
            or ""
        ).strip()
        if not raw:
            for ref in source_refs:
                raw = str(ref.get("uri") or "").strip()
                if raw:
                    break
        return (
            self._resolve_mineru_image_path(
                library_id=library_id,
                asset_id=asset_id,
                image_ref=raw,
            )
            or raw
        )

    def _resolved_source_refs(
        self,
        *,
        library_id: str,
        asset_id: str,
        refs: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(refs, list):
            return []
        resolved_refs: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            payload = dict(ref)
            if str(payload.get("kind") or "") == "image":
                raw_uri = str(payload.get("uri") or "").strip()
                resolved_uri = self._resolve_mineru_image_path(
                    library_id=library_id,
                    asset_id=asset_id,
                    image_ref=raw_uri,
                )
                if resolved_uri:
                    payload["uri"] = resolved_uri
                if not payload.get("source_image_id"):
                    payload["source_image_id"] = Path(raw_uri or resolved_uri or "").name
            resolved_refs.append(payload)
        return resolved_refs

    def _resolve_mineru_image_path(
        self,
        *,
        library_id: str,
        asset_id: str,
        image_ref: str,
    ) -> str | None:
        image_ref = str(image_ref or "").strip()
        if image_ref:
            path = Path(image_ref)
            if path.is_absolute():
                return str(path)
        if not library_id or not asset_id or not hasattr(self, "_config_path"):
            return None
        working_dir = self._resolve_working_dir()
        if working_dir is None:
            return None
        mineru_index = self._resolve_mineru_index(
            working_dir / library_id / "mineru_cache",
            asset_id,
        )
        if mineru_index is None:
            return None
        index_path, _index = mineru_index
        method_dir = index_path.parent
        if not image_ref:
            return None
        relative = Path(image_ref)
        candidates = [
            method_dir / relative,
            method_dir / "images" / relative.name,
            method_dir / "vlm" / "images" / relative.name,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        return None

    def _list_knowledgebase_records(
        self, *, library_ids: Collection[str] | None = None
    ) -> list[Any]:
        self._run_in_runtime_loop_sync(lambda: self._ensure_app()._ensure_core_runtime())
        _, get_knowledgebase_info_storage, _ = _load_hyperrag_storage(
            repo_root=self._repo_root,
            config_path=self._config_path,
        )
        storage = get_knowledgebase_info_storage()
        if library_ids is None:
            return self._run_in_runtime_loop_sync(storage.get_all)
        ids = list(dict.fromkeys(library_ids))
        if not ids:
            return []
        rows = self._run_in_runtime_loop_sync(lambda: storage.get_by_ids(ids))
        return [storage._to_record(row) for row in rows]

    def _build_library(self, record: Any) -> KnowledgeLibrary:
        stored_summary = normalize_text(getattr(record, "summarization", None))
        library_name = normalize_text(getattr(record, "name", None)) or str(record.id)
        asset_ids = list(getattr(record, "asset_ids", []) or [])
        updated_at = getattr(record, "updated_at", None)

        # Fast path: use pre-computed summary from HyperRAG indexing.
        # Avoids an expensive alist_assets network call per library.
        if stored_summary and stored_summary not in {EMPTY_KNOWLEDGEBASE_SUMMARY, ""}:
            return KnowledgeLibrary(
                id=str(record.id),
                name=library_name,
                description=stored_summary,
                metadata={
                    "source": "hyperrag",
                    "asset_ids": asset_ids,
                    "asset_count": len(asset_ids),
                    "completed_asset_count": None,
                    "updated_at": updated_at,
                },
            )

        # Slow path: summary is missing — fall back to building from asset statuses.
        _, _, asset_status_enum = _load_hyperrag_storage(
            repo_root=self._repo_root,
            config_path=self._config_path,
        )
        self._run_in_runtime_loop_sync(lambda: self._ensure_app()._ensure_core_runtime())
        status_map = self._run_in_runtime_loop_sync(
            lambda: self._ensure_app().alist_assets(record.id)
        )
        asset_summary_map = self._normalize_asset_summary_map(getattr(record, "metadata", None))

        active_statuses = [
            status
            for status in status_map.values()
            if getattr(status, "status", None) != asset_status_enum.DELETED
        ]
        completed_statuses = [
            status
            for status in active_statuses
            if getattr(status, "status", None) == asset_status_enum.COMPLETED
        ]
        completed_statuses.sort(key=lambda item: getattr(item, "updated_at", 0.0), reverse=True)

        asset_items = []
        for status in completed_statuses:
            summary_payload = asset_summary_map.get(str(status.asset_id), {})
            title = infer_asset_title(
                str(status.asset_id),
                getattr(status, "file_path", None),
                summary_payload.get("title"),
                getattr(status, "file_data", None),
            )
            summary = normalize_text(summary_payload.get("summary"))
            if not summary:
                summary = f"与{title}相关的内容已完成索引，可用于知识库检索与问答。"
            asset_items.append(
                {
                    "type_label": infer_asset_type_label(
                        getattr(status, "parse_mode", None),
                        getattr(status, "file_path", None),
                    ),
                    "title": title,
                    "summary": summary,
                }
            )

        description = compose_summary_from_assets(asset_items)
        return KnowledgeLibrary(
            id=str(record.id),
            name=library_name,
            description=description,
            metadata={
                "source": "hyperrag",
                "asset_ids": asset_ids,
                "asset_count": len(active_statuses),
                "completed_asset_count": len(completed_statuses),
                "updated_at": updated_at,
            },
        )

    @staticmethod
    def _normalize_asset_summary_map(metadata: Any) -> dict[str, dict[str, str]]:
        if not isinstance(metadata, dict):
            return {}
        raw = metadata.get("asset_summaries")
        if not isinstance(raw, dict):
            return {}

        normalized: dict[str, dict[str, str]] = {}
        for asset_id, payload in raw.items():
            key = normalize_text(asset_id)
            if not key or not isinstance(payload, dict):
                continue
            title = normalize_text(payload.get("title"))
            summary = normalize_text(payload.get("summary"))
            if not title and not summary:
                continue
            normalized[key] = {"title": title, "summary": summary}
        return normalized


def build_hyperrag_adapter(config: AppConfig) -> HyperRAGAdapter:
    return HyperRAGAdapter(config)


def _semantic_video_source_id(metadata: dict[str, Any]) -> str:
    sources = metadata.get("sources")
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if not isinstance(source, dict) or source.get("source_type") != "video":
            continue
        value = str(source.get("source_id") or "").strip()
        if value:
            return value
    return ""


def _overlapping_indexed_candidates(
    candidates: list[dict[str, Any]],
    *,
    start_seconds: float,
    end_seconds: float,
) -> list[dict[str, Any]]:
    """Return candidate facts whose indexed ranges overlap a video chunk."""
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate.get("time_range"), dict)
        and (candidate_end := candidate["time_range"].get("end_seconds")) is not None
        and (candidate_start := candidate["time_range"].get("start_seconds")) is not None
        and float(candidate_end) > start_seconds
        and float(candidate_start) < end_seconds
    ]


def _load_hyperrag_runtime(*, repo_root: Path | None, config_path: Path):
    _prepare_hyperrag_import_path(repo_root=repo_root, config_path=config_path)

    from hyperrag.config_loader import load_config_from_yaml
    from hyperrag.hyperrag_v2 import HyperRAGV2

    return load_config_from_yaml, HyperRAGV2


def _load_hyperrag_storage(*, repo_root: Path | None, config_path: Path):
    _prepare_hyperrag_import_path(repo_root=repo_root, config_path=config_path)

    from hyperrag.asset_status import AssetStatus
    from hyperrag.knowledgebase_info import get_knowledgebase_info_storage

    return None, get_knowledgebase_info_storage, AssetStatus


def _load_hyperrag_reranker(*, repo_root: Path | None, config_path: Path):
    _prepare_hyperrag_import_path(repo_root=repo_root, config_path=config_path)

    from hyperrag.rerank import OpenAICompatibleReranker, RerankCandidate

    return OpenAICompatibleReranker, RerankCandidate


def _prepare_hyperrag_import_path(*, repo_root: Path | None, config_path: Path) -> None:
    # Insert in reverse discovery order so the most specific candidate keeps the highest
    # import precedence. This ensures an explicit repo_root wins over ancestor fallbacks.
    for source_root in reversed(
        _iter_hyperrag_source_roots(repo_root=repo_root, config_path=config_path)
    ):
        source_root_text = str(source_root)
        if source_root_text not in sys.path:
            sys.path.insert(0, source_root_text)

        existing_package = sys.modules.get("hyperrag")
        if existing_package is None:
            continue

        package_path = str(source_root / "hyperrag")
        current_paths = getattr(existing_package, "__path__", None)
        if current_paths is None or package_path in current_paths:
            continue
        if hasattr(current_paths, "insert"):
            current_paths.insert(0, package_path)
            continue
        if hasattr(current_paths, "append"):
            current_paths.append(package_path)
            continue
        existing_package.__path__ = [package_path, *list(current_paths)]

    importlib.invalidate_caches()


def _iter_hyperrag_source_roots(*, repo_root: Path | None, config_path: Path) -> list[Path]:
    candidates: list[Path] = []

    if repo_root is not None:
        candidates.extend(_candidate_hyperrag_source_roots(Path(repo_root).resolve()))
        return candidates

    for parent in config_path.resolve().parents:
        candidates.extend(_candidate_hyperrag_source_roots(parent / "external" / "HyperRAG"))
        candidates.extend(_candidate_hyperrag_source_roots(parent / "HyperRAG"))

    seen: set[Path] = set()
    resolved: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)
    return resolved


def _candidate_hyperrag_source_roots(repo_root: Path) -> list[Path]:
    source_roots = [repo_root / "src", repo_root]
    return [
        source_root
        for source_root in source_roots
        if (source_root / "hyperrag" / "config_loader.py").is_file()
    ]


def _compile_mode_flags(
    compile_mode: Literal["pre_compile", "fast_ingest"] | None,
) -> tuple[bool, bool]:
    normalized = str(compile_mode or "").strip()
    if not normalized:
        return False, False
    if normalized == "fast_ingest":
        return True, False
    if normalized == "pre_compile":
        return False, True
    raise ValueError("compile_mode must be one of: None, 'fast_ingest', 'pre_compile'")
