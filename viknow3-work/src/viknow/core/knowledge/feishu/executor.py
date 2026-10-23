"""Shared manual and scheduled Feishu synchronization executor."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from uuid import NAMESPACE_URL, uuid4, uuid5

from loguru import logger

from viknow.core.knowledge.feishu.domain import (
    FeishuSyncItemStatus,
    FeishuSyncObject,
    FeishuSyncRun,
    FeishuSyncRunItem,
    FeishuSyncRunStatus,
    FeishuSyncSource,
    FeishuSyncStage,
    FeishuSyncTrigger,
    FeishuTrackingMode,
)
from viknow.core.knowledge.feishu.service import (
    FeishuKnowledgeService,
    FeishuKnowledgeStatus,
    FeishuMaterializedDocument,
)
from viknow.core.knowledge.feishu.store import FeishuSyncStore
from viknow.core.knowledge.uploads.domain import (
    KnowledgeUploadFailureKind,
    KnowledgeUploadRecord,
    KnowledgeUploadStatus,
)
from viknow.core.knowledge.uploads.manager import KnowledgeUploadManager
from viknow.core.session.mapping import now
from viknow.integrations.feishu import FeishuApiError


class FeishuSyncSourceBusyError(RuntimeError):
    """Raised when another process is starting the same source run."""

    def __init__(self, source_id: str) -> None:
        super().__init__(f"飞书同步来源正在由另一个实例处理: {source_id}")
        self.source_id = source_id


class FeishuSyncCoordinator:
    """Persist and reconcile per-document updates before the existing index path."""

    def __init__(
        self,
        *,
        service: FeishuKnowledgeService,
        upload_manager: KnowledgeUploadManager,
        store: FeishuSyncStore,
        now_provider: Callable[[], datetime] = now,
        default_interval_seconds: int = 10_800,
    ) -> None:
        self.service = service
        self.upload_manager = upload_manager
        self.store = store
        self._now = now_provider
        self._default_interval_seconds = default_interval_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    def ensure_source(
        self,
        *,
        node_tokens: list[str],
        tracking_mode: FeishuTrackingMode = FeishuTrackingMode.SELECTED,
        schedule_enabled: bool | None = None,
        interval_seconds: int | None = None,
    ) -> FeishuSyncSource:
        status, source_id, _ = self._import_scope(node_tokens, tracking_mode)
        with self.store.try_source_run_lock(source_id) as acquired:
            if not acquired:
                raise FeishuSyncSourceBusyError(source_id)
            return self._ensure_canonical_source(
                source_id, status, self._now(), schedule_enabled, interval_seconds
            )

    def _import_scope(
        self, node_tokens: list[str], tracking_mode: FeishuTrackingMode
    ) -> tuple[FeishuKnowledgeStatus, str, list[str]]:
        status = self.service.status()
        if not status.configured or not status.space_id:
            raise ValueError("飞书知识库配置不完整")
        requested_tokens = sorted({token.strip() for token in node_tokens if token.strip()})
        if tracking_mode == FeishuTrackingMode.SPACE and requested_tokens:
            raise ValueError("space tracking does not accept selected node tokens")
        normalized = [] if tracking_mode == FeishuTrackingMode.SPACE else requested_tokens
        if not normalized and tracking_mode != FeishuTrackingMode.SPACE:
            raise ValueError("node_tokens must not be empty")
        library_id = self.service.library_id()
        for previous in self.store.list_sources():
            if (
                previous.space_id == status.space_id
                and previous.library_id == library_id
                and previous.tracking_mode != FeishuTrackingMode.SPACE
            ):
                raise ValueError("存在历史飞书局部同步来源，需要单独审批迁移后再启用统一同步")
        return status, self._canonical_source_id(status), normalized

    def _canonical_source_id(self, status: FeishuKnowledgeStatus) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                f"feishu-source:{status.space_id}:{self.service.library_id()}:space:space",
            )
        )

    async def submit_import(
        self,
        *,
        node_tokens: list[str],
        tracking_mode: FeishuTrackingMode = FeishuTrackingMode.SELECTED,
        schedule_enabled: bool | None = None,
        interval_seconds: int | None = None,
        request_id: str | None = None,
    ) -> FeishuSyncRun:
        """Durably admit a manual request before attempting source registration."""
        _, source_id, tokens = self._import_scope(node_tokens, tracking_mode)
        timestamp = self._now()
        run = FeishuSyncRun(
            id=str(uuid4()),
            source_id=source_id,
            trigger=FeishuSyncTrigger.MANUAL,
            node_tokens=tokens,
            tracking_mode=tracking_mode,
            requested_schedule_enabled=schedule_enabled,
            requested_interval_seconds=interval_seconds,
            stage=FeishuSyncStage.QUEUED,
            started_at=timestamp,
            updated_at=timestamp,
        )
        self.store.add_run(run)
        return await self._execute_run(run.id, request_id=request_id)

    def _ensure_canonical_source(
        self,
        source_id: str,
        status: FeishuKnowledgeStatus,
        timestamp: datetime,
        schedule_enabled: bool | None,
        interval_seconds: int | None,
    ) -> FeishuSyncSource:
        existing = self.store.get_source(source_id)
        resolved_schedule_enabled = (
            True
            if schedule_enabled is True
            else existing.schedule_enabled
            if existing is not None
            else False
        )
        resolved_interval_seconds = (
            interval_seconds
            if interval_seconds is not None and schedule_enabled is True
            else existing.interval_seconds
            if existing is not None
            else self._default_interval_seconds
        )
        next_run_at = existing.next_run_at if existing is not None else None
        if resolved_schedule_enabled and next_run_at is None:
            next_run_at = timestamp
        if not resolved_schedule_enabled:
            next_run_at = None
        if resolved_schedule_enabled:
            self._assert_schedule_scope_available(
                source_id=source_id,
                space_id=status.space_id,
                library_id=self.service.library_id(),
                tracking_mode=FeishuTrackingMode.SPACE,
            )
        source = FeishuSyncSource(
            id=source_id,
            space_id=status.space_id,
            tenant_host=status.tenant_host,
            library_id=self.service.library_id(),
            node_tokens=[],
            tracking_mode=FeishuTrackingMode.SPACE,
            schedule_enabled=resolved_schedule_enabled,
            interval_seconds=resolved_interval_seconds,
            next_run_at=next_run_at,
            last_started_at=existing.last_started_at if existing else None,
            last_completed_at=existing.last_completed_at if existing else None,
            created_at=existing.created_at if existing else timestamp,
            updated_at=timestamp,
        )
        return self.store.upsert_source(source)

    def update_schedule(
        self,
        source_id: str,
        *,
        enabled: bool,
        interval_seconds: int,
    ) -> FeishuSyncSource:
        with self.store.try_source_run_lock(source_id) as acquired:
            if not acquired:
                raise FeishuSyncSourceBusyError(source_id)
            return self._update_schedule_locked(source_id, enabled, interval_seconds)

    def _update_schedule_locked(
        self, source_id: str, enabled: bool, interval_seconds: int
    ) -> FeishuSyncSource:
        source = self._require_source(source_id)
        if enabled:
            self._assert_schedule_scope_available(
                source_id=source.id,
                space_id=source.space_id,
                library_id=source.library_id,
                tracking_mode=source.tracking_mode,
            )
        timestamp = self._now()
        updated = source.model_copy(
            update={
                "schedule_enabled": enabled,
                "interval_seconds": interval_seconds,
                "next_run_at": timestamp if enabled else None,
                "updated_at": timestamp,
            }
        )
        return self.store.upsert_source(updated)

    async def run_source(
        self,
        source_id: str,
        *,
        trigger: FeishuSyncTrigger,
        request_id: str | None = None,
        node_tokens: list[str] | None = None,
        tracking_mode: FeishuTrackingMode | None = None,
        scheduled_deadline: datetime | None = None,
    ) -> FeishuSyncRun:
        self._require_source(source_id)
        tokens = sorted(set(node_tokens or []))
        mode = tracking_mode or (
            FeishuTrackingMode.SELECTED if tokens else FeishuTrackingMode.SPACE
        )
        if (mode == FeishuTrackingMode.SPACE) == bool(tokens):
            raise ValueError("飞书批次范围与同步模式不一致")
        async with self._try_source_run_lock(source_id) as acquired:
            if not acquired:
                raise FeishuSyncSourceBusyError(source_id)
            source = self._require_source(source_id)
            if scheduled_deadline is not None and (
                not source.schedule_enabled or source.next_run_at != scheduled_deadline
            ):
                raise FeishuSyncSourceBusyError(source_id)
            timestamp = self._now()
            deadline = source.next_run_at
            if trigger == FeishuSyncTrigger.SCHEDULED and deadline and deadline <= timestamp:
                interval = timedelta(seconds=source.interval_seconds)
                deadline += ((timestamp - deadline) // interval + 1) * interval
            active = next(
                (
                    run
                    for run in self.store.list_active_runs()
                    if run.source_id == source_id
                    and run.node_tokens == tokens
                    and run.tracking_mode == mode
                    and mode == FeishuTrackingMode.SELECTED
                    and run.items
                    and run.stage == FeishuSyncStage.INDEXING
                    and all(item.status == FeishuSyncItemStatus.WAITING for item in run.items)
                ),
                None,
            )
            if active is not None:
                return active
            run_id = (
                str(uuid5(NAMESPACE_URL, f"{source_id}:due:{scheduled_deadline.isoformat()}"))
                if scheduled_deadline is not None
                else str(uuid4())
            )
            run = self.store.get_run(run_id) or FeishuSyncRun(
                id=run_id,
                source_id=source.id,
                trigger=trigger,
                node_tokens=tokens,
                tracking_mode=mode,
                started_at=timestamp,
                updated_at=timestamp,
            )
            if self.store.get_run(run_id) is None:
                self.store.add_run(run)
            logger.bind(
                source_name="knowledge.feishu.sync",
                operation="run_source",
                result="started",
                source_id=source.id,
                sync_run_id=run.id,
                sync_trigger=trigger.value,
                request_id=request_id or "-",
            ).info("Feishu synchronization run started")
            source = self.store.upsert_source(
                source.model_copy(
                    update={
                        "last_started_at": timestamp,
                        "next_run_at": deadline,
                        "updated_at": timestamp,
                    }
                )
            )
        return await self._execute_run(run.id, request_id=request_id)

    async def _execute_run(self, run_id: str, *, request_id: str | None = None) -> FeishuSyncRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if self.store.get_source(run.source_id) is not None:
            self._require_source(run.source_id)
        else:
            status = self.service.status()
            if not status.configured or run.source_id != self._canonical_source_id(status):
                raise KeyError(run.source_id)
        async with self._try_source_run_lock(f"{run.source_id}:run:{run.id}") as acquired:
            if not acquired:
                return run
            run = self.store.get_run(run_id)
            if run.terminal:
                return run
            if run.stage == FeishuSyncStage.QUEUED:
                async with self._try_source_run_lock(run.source_id) as registered:
                    if not registered:
                        return run
                    try:
                        status, source_id, _ = self._import_scope(
                            run.node_tokens, run.tracking_mode
                        )
                        if source_id != run.source_id:
                            raise ValueError("飞书导入请求不属于当前配置的知识空间")
                    except ValueError as exc:
                        return self.store.update_run(
                            run.model_copy(
                                update={
                                    "status": FeishuSyncRunStatus.FAILED,
                                    "stage": FeishuSyncStage.COMPLETE,
                                    "error_message": str(exc),
                                    "completed_at": self._now(),
                                    "updated_at": self._now(),
                                }
                            )
                        )
                    source = self._ensure_canonical_source(
                        source_id,
                        status,
                        self._now(),
                        run.requested_schedule_enabled,
                        run.requested_interval_seconds,
                    )
                    self.store.upsert_source(
                        source.model_copy(update={"last_started_at": self._now()})
                    )
                    run = self.store.update_run(
                        run.model_copy(update={"stage": FeishuSyncStage.DISCOVERING})
                    )
            source = self._require_source(run.source_id)
            if run.stage != FeishuSyncStage.DISCOVERING:
                return await self._reconcile_run_locked(run, request_id=request_id)
            try:
                discovery = await self.service.discover_for_sync(
                    run.node_tokens,
                    tracking_mode=run.tracking_mode,
                )
            except Exception as exc:
                return self._fail_run(run, source, exc)

            run = self.store.update_run(
                run.model_copy(update={"items": [], "updated_at": self._now()})
            )
            discovered_tokens = {node.node_token for node in discovery.nodes}
            for node in discovery.nodes:
                selectable, _ = self.service.selection_support(node)
                if not selectable:
                    if node.has_child and run.tracking_mode != FeishuTrackingMode.SELECTED:
                        continue
                    run = self._append_item(
                        run,
                        FeishuSyncRunItem(
                            node_token=node.node_token,
                            title=node.title or "未命名文档",
                            status=FeishuSyncItemStatus.FAILED,
                            message=f"暂不支持 {node.obj_type} 类型或该文件格式",
                        ),
                    )
                    continue
                run = self._append_item(
                    run,
                    FeishuSyncRunItem(
                        node_token=node.node_token,
                        title=node.title,
                        status=FeishuSyncItemStatus.WAITING,
                    ),
                )

            missing_tokens = set(discovery.missing_node_tokens)
            for mapped in self.store.list_objects(source.id):
                confirmed_missing = mapped.node_token not in discovered_tokens
                if run.tracking_mode != FeishuTrackingMode.SPACE:
                    confirmed_missing = mapped.node_token in missing_tokens
                if not confirmed_missing:
                    continue
                run = self._append_item(
                    run,
                    FeishuSyncRunItem(
                        node_token=mapped.node_token,
                        title=mapped.title,
                        status=FeishuSyncItemStatus.WAITING,
                        delete_requested=True,
                        previous_upload_id=mapped.active_upload_id,
                        message="飞书已确认删除或移出同步范围",
                    ),
                )
            return await self._reconcile_run_locked(run, request_id=request_id)

    async def reconcile_run(self, run_id: str) -> FeishuSyncRun:
        return await self._execute_run(run_id)

    async def reconcile_active_runs(self) -> list[FeishuSyncRun]:
        reconciled: list[FeishuSyncRun] = []
        source_ids = {source.id for source in self.list_sources()}
        status = self.service.status()
        if status.configured and status.space_id:
            source_ids.add(self._canonical_source_id(status))
        for run in self.store.list_active_runs():
            if run.source_id not in source_ids:
                continue
            reconciled.append(await self.reconcile_run(run.id))
        return reconciled

    async def run_due_sources(self) -> list[FeishuSyncRun]:
        runs: list[FeishuSyncRun] = []
        library_id = self.service.library_id()
        for source in self.store.list_due_sources(self._now()):
            if source.library_id != library_id:
                continue
            try:
                runs.append(
                    await self.run_source(
                        source.id,
                        trigger=FeishuSyncTrigger.SCHEDULED,
                        scheduled_deadline=source.next_run_at,
                    )
                )
            except FeishuSyncSourceBusyError:
                logger.bind(
                    source_name="knowledge.feishu.sync",
                    operation="run_due_sources",
                    result="skipped_busy",
                    source_id=source.id,
                ).info("Feishu scheduled source is owned by another instance")
        return runs

    def list_sources(self) -> list[FeishuSyncSource]:
        library_id = self.service.library_id()
        return [source for source in self.store.list_sources() if source.library_id == library_id]

    def list_runs(self, source_id: str, *, limit: int = 50) -> list[FeishuSyncRun]:
        self._require_source(source_id)
        return self.store.list_runs(source_id, limit=limit)

    async def _reconcile_run_locked(
        self, run: FeishuSyncRun, *, request_id: str | None = None
    ) -> FeishuSyncRun:
        if run.terminal:
            return run
        run = self.store.update_run(run.model_copy(update={"stage": FeishuSyncStage.DOWNLOADING}))
        for position, item in enumerate(run.items):
            if item.status not in {FeishuSyncItemStatus.WAITING, FeishuSyncItemStatus.INDEXING}:
                continue
            try:
                advanced = await self._advance_item(run, item, request_id=request_id)
            except Exception as exc:
                advanced = self._failed_item(item.node_token, item.title, exc)
                self._record_object_error(run.source_id, item, exc)
            items = [*run.items]
            items[position] = advanced
            run = self.store.update_run(
                run.model_copy(update={"items": items, "updated_at": self._now()})
            )
        return self._finish_run_if_possible(run, self._require_source(run.source_id))

    async def _advance_item(
        self, run: FeishuSyncRun, item: FeishuSyncRunItem, *, request_id: str | None
    ) -> FeishuSyncRunItem:
        source = self._require_source(run.source_id)
        key = f"{source.id}:document:{item.node_token}"
        async with self._try_source_run_lock(key) as acquired:
            if not acquired:
                return item
            mapping = self.store.get_object(source.id, item.node_token)
            if item.status == FeishuSyncItemStatus.INDEXING:
                record = self.upload_manager.get_upload(item.upload_id or "")
                if record is None:
                    raise ValueError("待切换的上传记录不存在")
                self._check_upload_owner(source, item.node_token, record)
                if record.status == KnowledgeUploadStatus.DONE:
                    if mapping and mapping.pending_upload_id == record.id:
                        await self._activate_pending(mapping, record)
                    return item.model_copy(update={"status": FeishuSyncItemStatus.COMPLETED})
                if record.status == KnowledgeUploadStatus.FAILED:
                    raise ValueError(record.error_message or "飞书文档索引失败")
                return item
            if mapping and mapping.pending_upload_id:
                pending = self.upload_manager.get_upload(mapping.pending_upload_id)
                if pending:
                    self._check_upload_owner(source, item.node_token, pending)
                    if pending.status not in {
                        KnowledgeUploadStatus.DONE,
                        KnowledgeUploadStatus.FAILED,
                    }:
                        return item.model_copy(
                            update={
                                "upload_id": pending.id,
                                "message": "同文档正在处理，完成后复查最新内容",
                            }
                        )
                    if pending.status == KnowledgeUploadStatus.DONE:
                        mapping = await self._activate_pending(mapping, pending)
            if item.delete_requested:
                confirmation = await self.service.discover_for_sync(
                    [item.node_token], tracking_mode=FeishuTrackingMode.SELECTED
                )
                if item.node_token in confirmation.missing_node_tokens:
                    if mapping:
                        await self._delete_mapping(mapping)
                    return item.model_copy(update={"status": FeishuSyncItemStatus.DELETED})
            document = await self.service.materialize_for_sync(item.node_token)
            return await self._submit_document(
                source, run.id, run.trigger, document, request_id=request_id
            )

    @staticmethod
    def _check_upload_owner(
        source: FeishuSyncSource, node_token: str, record: KnowledgeUploadRecord
    ) -> None:
        if (
            record.library_id != source.library_id
            or record.source is None
            or record.source.type != "feishu"
            or record.source.space_id != source.space_id
            or record.source.node_token != node_token
        ):
            raise ValueError("上传记录不属于当前飞书空间，拒绝复用或修改")

    async def _submit_document(
        self,
        source: FeishuSyncSource,
        run_id: str,
        trigger: FeishuSyncTrigger,
        document: FeishuMaterializedDocument,
        *,
        request_id: str | None,
    ) -> FeishuSyncRunItem:
        digest = hashlib.sha256(document.content).hexdigest()
        mapping = self.store.get_object(source.id, document.node.node_token)
        if mapping is None:
            mapping = self._bootstrap_mapping(source, run_id, document, digest)
        if mapping:
            for upload_id in {mapping.active_upload_id, mapping.pending_upload_id} - {None}:
                owned = self.upload_manager.get_upload(upload_id)
                if owned:
                    self._check_upload_owner(source, document.node.node_token, owned)

        if mapping is not None and self._active_matches(mapping, document, digest):
            active = self.upload_manager.get_upload(mapping.active_upload_id or "")
            if active is not None and active.status == KnowledgeUploadStatus.DONE:
                self.store.upsert_object(
                    mapping.model_copy(
                        update={
                            "obj_token": document.node.obj_token,
                            "obj_type": document.node.obj_type,
                            "remote_change_hint": digest,
                            "last_synced_at": self._now(),
                            "last_error": None,
                            "provider_code": None,
                            "provider_http_status": None,
                            "updated_at": self._now(),
                        }
                    )
                )
                return FeishuSyncRunItem(
                    node_token=document.node.node_token,
                    title=document.node.title,
                    status=FeishuSyncItemStatus.UNCHANGED,
                    upload_id=active.id,
                    message="内容未变化，已跳过下载后的索引提交",
                )

        record: KnowledgeUploadRecord | None = None
        if mapping is not None and mapping.pending_content_digest == digest:
            record = self.upload_manager.get_upload(mapping.pending_upload_id or "")
            if (
                record is not None
                and record.status == KnowledgeUploadStatus.FAILED
                and record.failure_kind == KnowledgeUploadFailureKind.INDEX
            ):
                record = await self.upload_manager.retry_index(
                    record.id,
                    index_params=self.service.index_params_for(
                        node_token=document.node.node_token,
                        canonical_url=document.canonical_url,
                        source_id=source.id,
                        sync_run_id=run_id,
                        sync_trigger=trigger.value,
                    ),
                    source=self.service.upload_source_for(
                        node_token=document.node.node_token,
                        canonical_url=document.canonical_url,
                    ),
                    request_id=request_id,
                )

        if record is None:
            previous_upload_id = mapping.active_upload_id if mapping is not None else None
            version_key = (
                f"feishu-sync:{source.id}:{document.node.node_token}:{digest}:"
                f"{document.filename}:{previous_upload_id or 'initial'}"
            )
            upload_id = str(uuid5(NAMESPACE_URL, version_key))
            record = self.upload_manager.get_upload(upload_id)
            if record is None:
                record = await self.upload_manager.upload_stream(
                    filename=document.filename,
                    stream=BytesIO(document.content),
                    size_bytes=len(document.content),
                    content_type=mimetypes.guess_type(document.filename)[0],
                    upload_id=upload_id,
                    library_id=source.library_id,
                    index_params=self.service.index_params_for(
                        node_token=document.node.node_token,
                        canonical_url=document.canonical_url,
                        source_id=source.id,
                        sync_run_id=run_id,
                        sync_trigger=trigger.value,
                    ),
                    source=self.service.upload_source_for(
                        node_token=document.node.node_token,
                        canonical_url=document.canonical_url,
                    ),
                    request_id=request_id,
                )

        self._check_upload_owner(source, document.node.node_token, record)
        timestamp = self._now()
        mapping = self.store.upsert_object(
            FeishuSyncObject(
                source_id=source.id,
                node_token=document.node.node_token,
                obj_token=document.node.obj_token,
                obj_type=document.node.obj_type,
                title=document.node.title,
                remote_change_hint=digest,
                content_digest=mapping.content_digest if mapping is not None else None,
                active_upload_id=mapping.active_upload_id if mapping is not None else None,
                active_asset_id=mapping.active_asset_id if mapping is not None else None,
                pending_content_digest=digest,
                pending_upload_id=record.id,
                pending_run_id=run_id,
                last_synced_at=mapping.last_synced_at if mapping is not None else None,
                last_error=record.error_message,
                created_at=mapping.created_at if mapping is not None else timestamp,
                updated_at=timestamp,
            )
        )
        if record.status == KnowledgeUploadStatus.DONE:
            await self._activate_pending(mapping, record)
            return FeishuSyncRunItem(
                node_token=document.node.node_token,
                title=document.node.title,
                status=FeishuSyncItemStatus.COMPLETED,
                upload_id=record.id,
                previous_upload_id=mapping.active_upload_id,
            )
        if record.status == KnowledgeUploadStatus.FAILED:
            return FeishuSyncRunItem(
                node_token=document.node.node_token,
                title=document.node.title,
                status=FeishuSyncItemStatus.FAILED,
                upload_id=record.id,
                previous_upload_id=mapping.active_upload_id,
                message=record.error_message,
            )
        return FeishuSyncRunItem(
            node_token=document.node.node_token,
            title=document.node.title,
            status=FeishuSyncItemStatus.INDEXING,
            upload_id=record.id,
            previous_upload_id=mapping.active_upload_id,
            message="已提交主线索引任务",
        )

    def _bootstrap_mapping(
        self,
        source: FeishuSyncSource,
        run_id: str,
        document: FeishuMaterializedDocument,
        digest: str,
    ) -> FeishuSyncObject | None:
        legacy = next(
            (
                record
                for record in self.upload_manager.list_uploads(library_id=source.library_id)
                if record.source is not None
                and record.source.type == "feishu"
                and record.source.space_id == source.space_id
                and record.source.node_token == document.node.node_token
                and record.original_filename == document.filename
                and self.upload_manager.source_content_digest(record.id) == digest
            ),
            None,
        )
        if legacy is None:
            return None
        timestamp = self._now()
        active = legacy.status == KnowledgeUploadStatus.DONE
        return self.store.upsert_object(
            FeishuSyncObject(
                source_id=source.id,
                node_token=document.node.node_token,
                obj_token=document.node.obj_token,
                obj_type=document.node.obj_type,
                title=document.node.title,
                remote_change_hint=digest,
                content_digest=digest if active else None,
                active_upload_id=legacy.id if active else None,
                active_asset_id=legacy.asset_id if active else None,
                pending_content_digest=None if active else digest,
                pending_upload_id=None if active else legacy.id,
                pending_run_id=None if active else run_id,
                last_synced_at=timestamp if active else None,
                last_error=legacy.error_message,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    @staticmethod
    def _active_matches(
        mapping: FeishuSyncObject,
        document: FeishuMaterializedDocument,
        digest: str,
    ) -> bool:
        return (
            mapping.content_digest == digest
            and mapping.title == document.node.title
            and mapping.obj_type == document.node.obj_type
            and mapping.active_upload_id is not None
        )

    async def _activate_pending(
        self,
        mapping: FeishuSyncObject,
        record: KnowledgeUploadRecord,
    ) -> FeishuSyncObject:
        self._check_upload_owner(
            self._require_source(mapping.source_id), mapping.node_token, record
        )
        if mapping.pending_upload_id != record.id:
            raise ValueError("飞书索引结果不属于当前待切换版本")
        previous_upload_id = mapping.active_upload_id
        previous = (
            self.upload_manager.get_upload(previous_upload_id) if previous_upload_id else None
        )
        if previous:
            self._check_upload_owner(
                self._require_source(mapping.source_id), mapping.node_token, previous
            )
        activated = self.store.upsert_object(
            mapping.model_copy(
                update={
                    "content_digest": mapping.pending_content_digest,
                    "active_upload_id": record.id,
                    "active_asset_id": record.asset_id,
                    "pending_content_digest": None,
                    "pending_upload_id": None,
                    "pending_run_id": None,
                    "last_synced_at": self._now(),
                    "last_error": None,
                    "provider_code": None,
                    "provider_http_status": None,
                    "updated_at": self._now(),
                }
            )
        )
        if (
            previous_upload_id
            and previous_upload_id != record.id
            and not self._upload_has_other_owners(
                source_id=mapping.source_id,
                upload_id=previous_upload_id,
            )
        ):
            await self.upload_manager.delete_upload(previous_upload_id)
        return activated

    async def _delete_mapping(self, mapping: FeishuSyncObject) -> None:
        source = self._require_source(mapping.source_id)
        for upload_id in {mapping.active_upload_id, mapping.pending_upload_id} - {None}:
            record = self.upload_manager.get_upload(upload_id)
            if record:
                self._check_upload_owner(source, mapping.node_token, record)
        current = mapping
        pending_upload_id = current.pending_upload_id
        if (
            pending_upload_id
            and pending_upload_id != current.active_upload_id
            and not self._upload_has_other_owners(
                source_id=current.source_id,
                upload_id=pending_upload_id,
            )
        ):
            await self.upload_manager.delete_upload(pending_upload_id)
        if pending_upload_id:
            current = self.store.upsert_object(
                current.model_copy(
                    update={
                        "pending_content_digest": None,
                        "pending_upload_id": None,
                        "pending_run_id": None,
                        "updated_at": self._now(),
                    }
                )
            )

        active_upload_id = current.active_upload_id
        if active_upload_id:
            if not self._upload_has_other_owners(
                source_id=current.source_id,
                upload_id=active_upload_id,
            ):
                await self.upload_manager.delete_upload(active_upload_id)
            current = self.store.upsert_object(
                current.model_copy(
                    update={
                        "content_digest": None,
                        "active_upload_id": None,
                        "active_asset_id": None,
                        "updated_at": self._now(),
                    }
                )
            )
        self.store.delete_object(current.source_id, current.node_token)

    def _upload_has_other_owners(self, *, source_id: str, upload_id: str) -> bool:
        for source in self.store.list_sources():
            if source.id == source_id:
                continue
            for mapping in self.store.list_objects(source.id):
                if upload_id in {mapping.active_upload_id, mapping.pending_upload_id}:
                    return True
        return False

    def _finish_run_if_possible(
        self,
        run: FeishuSyncRun,
        source: FeishuSyncSource,
    ) -> FeishuSyncRun:
        if any(
            item.status in {FeishuSyncItemStatus.WAITING, FeishuSyncItemStatus.INDEXING}
            for item in run.items
        ):
            indexing = self.store.update_run(
                run.model_copy(
                    update={
                        "status": FeishuSyncRunStatus.INDEXING,
                        "stage": FeishuSyncStage.INDEXING,
                        "updated_at": self._now(),
                    }
                )
            )
            logger.bind(
                source_name="knowledge.feishu.sync",
                operation="run_source",
                result="indexing",
                source_id=source.id,
                sync_run_id=run.id,
                sync_trigger=run.trigger.value,
                item_count=len(run.items),
            ).info("Feishu synchronization run is waiting for index completion")
            return indexing
        failed = sum(item.status == FeishuSyncItemStatus.FAILED for item in run.items)
        if failed == 0:
            status = FeishuSyncRunStatus.COMPLETED
        elif failed == len(run.items):
            status = FeishuSyncRunStatus.FAILED
        else:
            status = FeishuSyncRunStatus.PARTIAL_FAILED
        timestamp = self._now()
        self._complete_source(source, timestamp)
        completed = self.store.update_run(
            run.model_copy(
                update={
                    "status": status,
                    "stage": FeishuSyncStage.COMPLETE,
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                }
            )
        )
        logger.bind(
            source_name="knowledge.feishu.sync",
            operation="run_source",
            result=completed.status.value,
            source_id=source.id,
            sync_run_id=completed.id,
            sync_trigger=completed.trigger.value,
            item_count=len(completed.items),
            failed_count=failed,
        ).info("Feishu synchronization run completed")
        return completed

    def _fail_run(
        self,
        run: FeishuSyncRun,
        source: FeishuSyncSource,
        exc: Exception,
    ) -> FeishuSyncRun:
        message, code, http_status, retryable = self._error_fields(exc)
        timestamp = self._now()
        self._complete_source(source, timestamp)
        failed = self.store.update_run(
            run.model_copy(
                update={
                    "status": FeishuSyncRunStatus.FAILED,
                    "stage": FeishuSyncStage.COMPLETE,
                    "error_message": message,
                    "provider_code": code,
                    "provider_http_status": http_status,
                    "retryable": retryable,
                    "completed_at": timestamp,
                    "updated_at": timestamp,
                }
            )
        )
        logger.bind(
            source_name="knowledge.feishu.sync",
            operation="run_source",
            result="failed",
            source_id=source.id,
            sync_run_id=failed.id,
            sync_trigger=failed.trigger.value,
            error_class=type(exc).__name__,
            cause_class=type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
            provider_code=code,
            provider_http_status=http_status,
            retryable=retryable,
        ).warning("Feishu synchronization run failed")
        return failed

    def _complete_source(self, source: FeishuSyncSource, timestamp: datetime) -> None:
        with self.store.try_source_run_lock(source.id) as acquired:
            if not acquired:
                raise FeishuSyncSourceBusyError(source.id)
            current = self._require_source(source.id)
            self.store.upsert_source(
                current.model_copy(update={"last_completed_at": timestamp, "updated_at": timestamp})
            )

    def _append_item(
        self,
        run: FeishuSyncRun,
        item: FeishuSyncRunItem,
    ) -> FeishuSyncRun:
        return self.store.update_run(
            run.model_copy(update={"items": [*run.items, item], "updated_at": self._now()})
        )

    def _failed_item(
        self,
        node_token: str,
        title: str,
        exc: Exception,
    ) -> FeishuSyncRunItem:
        message, code, http_status, retryable = self._error_fields(exc)
        return FeishuSyncRunItem(
            node_token=node_token,
            title=title or "未命名文档",
            status=FeishuSyncItemStatus.FAILED,
            message=message,
            retryable=retryable,
            provider_code=code,
            provider_http_status=http_status,
        )

    def _record_object_error(self, source_id: str, node, exc: Exception) -> None:
        mapping = self.store.get_object(source_id, node.node_token)
        if mapping is None:
            return
        message, code, http_status, _ = self._error_fields(exc)
        self.store.upsert_object(
            mapping.model_copy(
                update={
                    "last_error": message,
                    "provider_code": code,
                    "provider_http_status": http_status,
                    "updated_at": self._now(),
                }
            )
        )

    @staticmethod
    def _error_fields(exc: Exception) -> tuple[str, int | None, int | None, bool]:
        if isinstance(exc, FeishuApiError):
            return str(exc), exc.code, exc.http_status, exc.retryable
        return str(exc), None, None, False

    def _require_source(self, source_id: str) -> FeishuSyncSource:
        source = self.store.get_source(source_id)
        if (
            source is None
            or source.library_id != self.service.library_id()
            or source.space_id != self.service.status().space_id
        ):
            raise KeyError(source_id)
        if source.tracking_mode != FeishuTrackingMode.SPACE:
            raise ValueError("历史飞书局部同步来源需要单独审批迁移")
        return source

    def _assert_schedule_scope_available(
        self,
        *,
        source_id: str,
        space_id: str,
        library_id: str,
        tracking_mode: FeishuTrackingMode,
    ) -> None:
        for other in self.store.list_sources():
            if (
                other.id == source_id
                or other.space_id != space_id
                or other.library_id != library_id
                or not other.schedule_enabled
            ):
                continue
            if (
                tracking_mode == FeishuTrackingMode.SPACE
                or other.tracking_mode == FeishuTrackingMode.SPACE
            ):
                raise ValueError("同一飞书知识空间不能同时启用全空间和局部定时同步")

    def _lock(self, source_id: str) -> asyncio.Lock:
        return self._locks.setdefault(source_id, asyncio.Lock())

    @asynccontextmanager
    async def _try_source_run_lock(self, source_id: str) -> AsyncIterator[bool]:
        if self._lock(source_id).locked():
            yield False
            return
        async with self._lock(source_id):
            with self.store.try_source_run_lock(source_id) as acquired:
                yield acquired


__all__ = ["FeishuSyncCoordinator", "FeishuSyncSourceBusyError"]
