"""Orchestration for knowledge library streamed upload and indexing."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import shutil
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from uuid import uuid4

from actio.global_context import normalize_global_context
from loguru import logger as structured_logger

from viknow.core.config import KnowledgeConfig, resolve_knowledge_uploads_root
from viknow.core.knowledge.media_normalize import (
    VideoNormalizeError,
    is_video_path,
    normalize_video_async,
    normalized_output_path,
)
from viknow.core.knowledge.uploads.domain import (
    FAILURE_MESSAGES,
    KnowledgeUploadFailureKind,
    KnowledgeUploadRecord,
    KnowledgeUploadSource,
    KnowledgeUploadStatus,
)
from viknow.core.knowledge.uploads.names import allocate_display_name, normalize_extension
from viknow.core.knowledge.uploads.store import KnowledgeUploadStore
from viknow.core.session.mapping import now

logger = logging.getLogger(__name__)


def resolve_upload_library_id(library_id: str | None, default_library_id: str) -> str:
    if library_id is None:
        return default_library_id
    cleaned = str(library_id).strip()
    if not cleaned:
        raise ValueError("library_id must not be empty")
    if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned or "\0" in cleaned:
        raise ValueError("library_id is invalid")
    return cleaned


class KnowledgeIndexAdapter(Protocol):
    default_library_id: str

    async def submit_index_async(
        self,
        *,
        library_id: str,
        asset_id: str,
        asset_path: str,
        compile_mode: Literal["pre_compile", "fast_ingest"] | None = None,
        params: dict | None = None,
        request_id: str | None = None,
    ): ...

    async def delete_index_async(self, *, library_id: str, asset_id: str): ...

    async def get_video_global_summary_async(self, *, library_id: str, asset_id: str) -> str: ...

    async def get_video_summary_async(self, *, library_id: str, asset_id: str) -> dict: ...

    async def update_video_global_summary_async(
        self,
        *,
        library_id: str,
        asset_id: str,
        global_summary: str,
        global_context: dict | None = None,
        request_id: str | None = None,
    ) -> str: ...

    def get_asset_status(self, *, library_id: str, asset_id: str): ...

    def list_libraries(self) -> list: ...


class KnowledgeUploadManager:
    """Product upload registry + streamed write to the files root + HyperRAG index bridge.

    Upload bytes are the source of truth on the shared path tree under
    ``{files_root}/knowledge/uploads/{library_id}/``; records store the path
    relative to the files root and indexing reads that same path.
    """

    def __init__(
        self,
        *,
        store: KnowledgeUploadStore,
        files_root: Path,
        knowledge_config: KnowledgeConfig,
        knowledge_adapter: KnowledgeIndexAdapter,
    ) -> None:
        self._store = store
        self._files_root = files_root
        self._knowledge_config = knowledge_config
        self._adapter = knowledge_adapter

    async def upload_stream(
        self,
        *,
        filename: str,
        stream: BinaryIO,
        size_bytes: int | None = None,
        content_type: str | None = None,
        upload_id: str | None = None,
        library_id: str | None = None,
        index_params: dict | None = None,
        source: KnowledgeUploadSource | None = None,
        request_id: str | None = None,
    ) -> KnowledgeUploadRecord:
        """Stream file bytes onto the files root, then enqueue HyperRAG indexing.

        ``library_id`` defaults to ``knowledge.default_library_id`` when omitted
        (UI upload path). Callers such as Postman may pass an explicit library.
        """

        declared_size = int(size_bytes) if size_bytes is not None and size_bytes > 0 else 0
        library_id = self.resolve_upload_library_id(library_id)
        self.ensure_library(library_id)
        display_name = allocate_display_name(
            Path(filename).name,
            self._store.list_display_names(library_id=library_id),
        )
        upload_id = upload_id or str(uuid4())
        if self._store.get(upload_id, include_deleted=True) is not None:
            raise ValueError("upload_id already exists")
        suffix = normalize_extension(filename)
        source_path = self._source_path(library_id=library_id, upload_id=upload_id, suffix=suffix)
        object_key = self._storage_relative(source_path)
        timestamp = now()
        record = KnowledgeUploadRecord(
            id=upload_id,
            library_id=library_id,
            asset_id=upload_id,
            display_name=display_name,
            original_filename=Path(filename).name,
            object_key=object_key,
            size_bytes=declared_size,
            content_type=content_type,
            status=KnowledgeUploadStatus.UPLOADING,
            source=source,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._store.add(record)

        try:
            await asyncio.to_thread(self._write_source_file, source_path, stream)
        except Exception:
            return self._mark_failed(record, KnowledgeUploadFailureKind.UPLOAD)

        return await self.complete_upload(
            upload_id,
            index_params=index_params,
            request_id=request_id,
        )

    async def complete_upload(
        self,
        upload_id: str,
        *,
        index_params: dict | None = None,
        request_id: str | None = None,
    ) -> KnowledgeUploadRecord:
        record = self._require_record(upload_id)
        if record.status == KnowledgeUploadStatus.DONE:
            return record
        if record.status == KnowledgeUploadStatus.INDEXING:
            return self.refresh_record(record)
        if record.status == KnowledgeUploadStatus.FAILED:
            return record
        if record.status not in {
            KnowledgeUploadStatus.UPLOADING,
            KnowledgeUploadStatus.NORMALIZING,
        }:
            raise ValueError(f"upload {upload_id} cannot be completed from status={record.status}")

        source_path = self._absolute_path(record.object_key)
        if not source_path.is_file():
            raise FileNotFoundError(str(source_path))
        object_size = source_path.stat().st_size
        if object_size <= 0:
            return self._mark_failed(record, KnowledgeUploadFailureKind.UPLOAD)

        index_path = source_path

        if is_video_path(source_path):
            record = self._store.update(
                record.model_copy(
                    update={
                        "status": KnowledgeUploadStatus.NORMALIZING,
                        "local_path": str(source_path),
                        "size_bytes": object_size,
                        "failure_kind": None,
                        "error_message": None,
                        "updated_at": now(),
                    }
                )
            )
            try:
                index_path = await normalize_video_async(
                    source_path,
                    normalized_output_path(source_path),
                )
            except (VideoNormalizeError, OSError):
                return self._mark_failed(record, KnowledgeUploadFailureKind.NORMALIZE)

        self.ensure_library(record.library_id)
        try:
            structured_logger.bind(
                source_name="knowledge.index",
                operation="submit_index",
                upload_id=record.id,
                asset_id=record.asset_id,
                library_id=record.library_id,
                request_id=request_id or "-",
            ).info("Submitting knowledge index job")
            accepted = await self._adapter.submit_index_async(
                library_id=record.library_id,
                asset_id=record.asset_id,
                asset_path=str(index_path),
                compile_mode="fast_ingest",
                params={"title": record.display_name, **(index_params or {})},
                request_id=request_id,
            )
        except Exception as exc:
            structured_logger.bind(
                source_name="knowledge.index",
                operation="submit_index",
                result="failed",
                upload_id=record.id,
                asset_id=record.asset_id,
                library_id=record.library_id,
                request_id=request_id or "-",
                error_class=type(exc).__name__,
            ).opt(exception=True).error("Knowledge index submission failed")
            return self._mark_failed(
                record,
                KnowledgeUploadFailureKind.INDEX,
                error_message=str(exc),
            )

        structured_logger.bind(
            source_name="knowledge.index",
            operation="submit_index",
            result="accepted",
            upload_id=record.id,
            asset_id=record.asset_id,
            library_id=record.library_id,
            job_id=accepted.job_id,
            request_id=request_id or "-",
        ).info("Knowledge index job accepted")

        updated = record.model_copy(
            update={
                "status": KnowledgeUploadStatus.INDEXING,
                "local_path": str(index_path),
                "size_bytes": object_size,
                "job_id": accepted.job_id,
                "failure_kind": None,
                "error_message": None,
                "updated_at": now(),
            }
        )
        return self._store.update(updated)

    async def retry_index(
        self,
        upload_id: str,
        *,
        index_params: dict | None = None,
        source: KnowledgeUploadSource | None = None,
        request_id: str | None = None,
    ) -> KnowledgeUploadRecord:
        """Retry failed normalization or indexing while reusing persisted source bytes."""

        record = self._require_record(upload_id)
        if source is not None and record.source != source:
            record = self._store.update(
                record.model_copy(update={"source": source, "updated_at": now()})
            )
        if record.status != KnowledgeUploadStatus.FAILED or record.failure_kind not in {
            KnowledgeUploadFailureKind.INDEX,
            KnowledgeUploadFailureKind.NORMALIZE,
        }:
            return record
        reset = self._store.update(
            record.model_copy(
                update={
                    "status": KnowledgeUploadStatus.UPLOADING,
                    "failure_kind": None,
                    "error_message": None,
                    "updated_at": now(),
                }
            )
        )
        return await self.complete_upload(
            reset.id,
            index_params=index_params,
            request_id=request_id,
        )

    def evidence_metadata(self, *, library_id: str, asset_id: str) -> dict[str, str | None]:
        record = self._store.get(asset_id)
        if record is None or record.library_id != library_id or record.source is None:
            return {}
        source = record.source
        return {
            "title": record.display_name,
            "asset_id": record.asset_id,
            "source_type": source.type,
            "source_url": source.url,
            "source_node_token": source.node_token,
            "source_space_id": source.space_id,
        }

    def get_upload(self, upload_id: str) -> KnowledgeUploadRecord | None:
        record = self._store.get(upload_id)
        if record is None:
            return None
        return self.refresh_record(record)

    def list_uploads(self, *, library_id: str | None = None) -> list[KnowledgeUploadRecord]:
        library_id = self.resolve_upload_library_id(library_id)
        records = self._store.list_for_library(library_id=library_id)
        return [self.refresh_record(record) for record in records]

    def source_content_digest(self, upload_id: str) -> str | None:
        """Return the persisted source-file digest without changing upload state."""

        record = self._store.get(upload_id)
        if record is None:
            return None
        path = self._absolute_path(record.object_key)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def delete_upload(self, upload_id: str) -> bool:
        record = self._store.get(upload_id, include_deleted=True)
        if record is None:
            return False
        if record.deleted_at is not None:
            return True
        if record.status in {
            KnowledgeUploadStatus.INDEXING,
            KnowledgeUploadStatus.DONE,
            KnowledgeUploadStatus.FAILED,
        }:
            # Soft-delete still proceeds; index cleanup is best-effort.
            with contextlib.suppress(Exception):
                await self._adapter.delete_index_async(
                    library_id=record.library_id,
                    asset_id=record.asset_id,
                )
        deleted = self._store.soft_delete(upload_id)
        if deleted:
            self._delete_source_files(record)
        return deleted

    async def get_video_global_summary(self, upload_id: str) -> dict:
        record = self.refresh_record(self._require_record(upload_id))
        self._ensure_video_summary_editable(record)
        summary = await self._adapter.get_video_summary_async(
            library_id=record.library_id,
            asset_id=record.asset_id,
        )
        return {
            "upload_id": record.id,
            "library_id": record.library_id,
            "asset_id": record.asset_id,
            "global_summary": summary["global_summary"],
            "global_context": normalize_global_context(summary.get("global_context")),
        }

    async def update_video_global_summary(
        self,
        *,
        upload_id: str,
        global_summary: str,
        global_context: dict | None = None,
        request_id: str | None = None,
    ) -> tuple[bool, str | None, KnowledgeUploadRecord]:
        record = self.refresh_record(self._require_record(upload_id))
        self._ensure_video_summary_editable(record)
        normalized_summary = str(global_summary or "").strip()
        if not normalized_summary:
            raise ValueError("global_summary must not be empty")
        current = await self._adapter.get_video_summary_async(
            library_id=record.library_id,
            asset_id=record.asset_id,
        )
        current_context = normalize_global_context(current.get("global_context"))
        normalized_context = (
            current_context if global_context is None else normalize_global_context(global_context)
        )
        if (
            normalized_summary == current["global_summary"]
            and normalized_context == current_context
        ):
            return False, None, record
        job_id = await self._adapter.update_video_global_summary_async(
            library_id=record.library_id,
            asset_id=record.asset_id,
            global_summary=normalized_summary,
            global_context=normalized_context if global_context is not None else None,
            request_id=request_id,
        )
        return True, job_id, record

    def resolve_upload_library_id(self, library_id: str | None) -> str:
        return resolve_upload_library_id(library_id, self._knowledge_config.default_library_id)

    def ensure_library(self, library_id: str) -> str:
        # HyperRAG creates workspace state on first aadd_asset; presence in
        # knowledgebase_info is filled after indexing. Calling list_libraries
        # here turns an upload precondition into an unnecessary database probe.
        return self.resolve_upload_library_id(library_id)

    def ensure_default_library(self) -> str:
        return self.ensure_library(self._knowledge_config.default_library_id)

    def refresh_record(self, record: KnowledgeUploadRecord) -> KnowledgeUploadRecord:
        if record.status != KnowledgeUploadStatus.INDEXING:
            return record
        status = self._adapter.get_asset_status(
            library_id=record.library_id,
            asset_id=record.asset_id,
        )
        if status is None:
            return record
        raw_status = getattr(status, "status", None)
        normalized = str(getattr(raw_status, "value", raw_status) or "").lower()
        if normalized == "completed":
            updated = record.model_copy(
                update={
                    "status": KnowledgeUploadStatus.DONE,
                    "failure_kind": None,
                    "error_message": None,
                    "updated_at": now(),
                }
            )
            return self._store.update(updated)
        if normalized in {"failed", "cancelled", "canceled"}:
            error_message = str(getattr(status, "error_message", "") or "").strip()
            structured_logger.bind(
                source_name="knowledge.index",
                operation="refresh_status",
                result=normalized,
                upload_id=record.id,
                asset_id=record.asset_id,
                library_id=record.library_id,
                job_id=record.job_id or "-",
                index_error=error_message or FAILURE_MESSAGES[KnowledgeUploadFailureKind.INDEX],
            ).error("Knowledge index job failed")
            return self._mark_failed(
                record,
                KnowledgeUploadFailureKind.INDEX,
                error_message=error_message or None,
            )
        return record

    def _mark_failed(
        self,
        record: KnowledgeUploadRecord,
        kind: KnowledgeUploadFailureKind,
        *,
        error_message: str | None = None,
    ) -> KnowledgeUploadRecord:
        updated = record.model_copy(
            update={
                "status": KnowledgeUploadStatus.FAILED,
                "failure_kind": kind,
                "error_message": error_message or FAILURE_MESSAGES[kind],
                "updated_at": now(),
            }
        )
        return self._store.update(updated)

    def _source_path(self, *, library_id: str, upload_id: str, suffix: str) -> Path:
        uploads_root = resolve_knowledge_uploads_root(self._files_root)
        return uploads_root / library_id / f"{upload_id}{suffix}"

    def _storage_relative(self, path: Path) -> str:
        return path.relative_to(self._files_root).as_posix()

    def _absolute_path(self, storage_relative: str) -> Path:
        return self._files_root / storage_relative

    @staticmethod
    def _write_source_file(target: Path, stream: BinaryIO) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            shutil.copyfileobj(stream, handle)

    def _delete_source_files(self, record: KnowledgeUploadRecord) -> None:
        """Drop upload bytes on the path tree; index/cache cleanup is the adapter's job."""

        candidates = [self._absolute_path(record.object_key)]
        if record.local_path:
            candidates.append(Path(record.local_path))
        uploads_root = resolve_knowledge_uploads_root(self._files_root)
        for path in candidates:
            if not path.is_relative_to(uploads_root):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "failed to delete knowledge upload source upload_id=%s path=%s",
                    record.id,
                    path,
                )

    def _require_record(self, upload_id: str) -> KnowledgeUploadRecord:
        record = self._store.get(upload_id)
        if record is None:
            raise KeyError(upload_id)
        return record

    def _ensure_video_summary_editable(self, record: KnowledgeUploadRecord) -> None:
        if record.status != KnowledgeUploadStatus.DONE:
            raise ValueError("仅支持已完成索引的视频编辑摘要")
        status = self._adapter.get_asset_status(
            library_id=record.library_id,
            asset_id=record.asset_id,
        )
        parse_mode = str(getattr(status, "parse_mode", "") or "").strip().lower()
        if parse_mode != "video":
            raise ValueError("仅支持普通视频索引（parse_mode=video）编辑摘要")
