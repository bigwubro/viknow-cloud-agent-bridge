"""HTTP boundary for read-only Feishu Wiki discovery and selection preview."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.knowledge.feishu import FeishuKnowledgeService, build_feishu_knowledge_service
from viknow.core.knowledge.feishu.domain import (
    FeishuSyncItemStatus,
    FeishuSyncRun,
    FeishuSyncRunStatus,
    FeishuSyncSource,
    FeishuSyncTrigger,
    FeishuTrackingMode,
)
from viknow.core.knowledge.feishu.executor import (
    FeishuSyncCoordinator,
    FeishuSyncSourceBusyError,
)
from viknow.core.knowledge.uploads.domain import KnowledgeUploadRecord
from viknow.core.knowledge.uploads.manager import KnowledgeUploadManager
from viknow.integrations.feishu import (
    FeishuApiError,
    FeishuConfigurationError,
    FeishuWikiNode,
)


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeishuKnowledgeStatusResponse(_ApiModel):
    enabled: bool
    configured: bool
    auth_mode: Literal["tenant_access_token"] = "tenant_access_token"
    space_id: str | None = None
    tenant_host: str | None = None
    missing_env_names: list[str] = Field(default_factory=list)
    default_sync_interval_seconds: int


class FeishuKnowledgeNodeResponse(_ApiModel):
    node_token: str
    title: str
    obj_type: str
    has_child: bool
    parent_node_token: str | None = None
    selectable: bool
    selection_reason: str


class FeishuKnowledgeNodeListResponse(_ApiModel):
    space_id: str
    space_name: str
    items: list[FeishuKnowledgeNodeResponse]
    has_more: bool = False
    page_token: str | None = None


class FeishuSelectionPreviewRequest(_ApiModel):
    node_tokens: list[str] = Field(min_length=1, max_length=100)


class FeishuSelectionPreviewItemResponse(_ApiModel):
    node_token: str
    status: Literal[
        "valid",
        "duplicate",
        "unsupported",
        "cross_space",
        "permission_denied",
        "invalid",
    ]
    title: str | None = None
    space_id: str | None = None
    obj_token: str | None = None
    obj_type: str | None = None
    canonical_url: str | None = None
    message: str


class FeishuSelectionPreviewResponse(_ApiModel):
    space_id: str
    valid_count: int
    items: list[FeishuSelectionPreviewItemResponse]


class FeishuImportRequest(_ApiModel):
    node_tokens: list[str] = Field(default_factory=list, max_length=100)
    tracking_mode: FeishuTrackingMode = FeishuTrackingMode.SELECTED
    schedule_enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)

    @model_validator(mode="after")
    def validate_tracking_scope(self) -> FeishuImportRequest:
        if self.tracking_mode == FeishuTrackingMode.SPACE and self.node_tokens:
            raise ValueError("space tracking does not accept selected node tokens")
        if self.tracking_mode != FeishuTrackingMode.SPACE and not self.node_tokens:
            raise ValueError("node_tokens must not be empty for selected or subtree tracking")
        return self


class FeishuImportUploadResponse(_ApiModel):
    source_type: Literal["feishu"] = "feishu"
    upload_id: str
    library_id: str
    asset_id: str
    display_name: str
    original_filename: str
    size_bytes: int
    status: str
    failure_kind: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class FeishuImportResponse(_ApiModel):
    space_id: str
    library_id: str
    uploads: list[FeishuImportUploadResponse]
    source_id: str | None = None
    run_id: str | None = None
    sync_status: FeishuSyncRunStatus | None = None
    waiting_count: int = 0
    schedule_enabled: bool = False
    interval_seconds: int | None = None
    next_run_at: datetime | None = None


class FeishuSyncRunItemResponse(_ApiModel):
    node_token: str
    title: str
    status: FeishuSyncItemStatus
    upload_id: str | None = None
    previous_upload_id: str | None = None
    message: str | None = None
    retryable: bool = False
    provider_code: int | None = None
    provider_http_status: int | None = None


class FeishuSyncRunResponse(_ApiModel):
    id: str
    source_id: str
    trigger: FeishuSyncTrigger
    status: FeishuSyncRunStatus
    stage: str
    items: list[FeishuSyncRunItemResponse]
    error_message: str | None = None
    provider_code: int | None = None
    provider_http_status: int | None = None
    retryable: bool = False
    started_at: datetime
    completed_at: datetime | None = None
    updated_at: datetime


class FeishuSyncSourceResponse(_ApiModel):
    id: str
    space_id: str
    library_id: str
    node_tokens: list[str]
    tracking_mode: FeishuTrackingMode
    schedule_enabled: bool
    interval_seconds: int
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None


class FeishuSyncSourceListResponse(_ApiModel):
    items: list[FeishuSyncSourceResponse]


class FeishuSyncRunListResponse(_ApiModel):
    items: list[FeishuSyncRunResponse]


class FeishuScheduleUpdateRequest(_ApiModel):
    enabled: bool
    interval_seconds: int = Field(ge=60, le=31_536_000)


def create_feishu_knowledge_router(
    *,
    upload_manager_provider: Callable[[Request], KnowledgeUploadManager],
    sync_coordinator_provider: Callable[[Request], FeishuSyncCoordinator],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/knowledge/feishu", tags=["feishu-knowledge"])

    @router.get("/status", response_model=FeishuKnowledgeStatusResponse)
    async def get_status(request: Request) -> FeishuKnowledgeStatusResponse:
        status = request_feishu_knowledge_service(request).status()
        return FeishuKnowledgeStatusResponse.model_validate(status.model_dump())

    @router.get("/nodes", response_model=FeishuKnowledgeNodeListResponse)
    async def list_nodes(
        request: Request,
        parent_node_token: str | None = Query(default=None, min_length=6, max_length=128),
        page_token: str | None = Query(default=None, max_length=512),
    ) -> FeishuKnowledgeNodeListResponse:
        started_at = perf_counter()
        try:
            page = await request_feishu_knowledge_service(request).list_nodes(
                parent_node_token=parent_node_token,
                page_token=page_token,
            )
        except (FeishuConfigurationError, FeishuApiError, ValueError) as exc:
            _log_operation_failure("list_nodes", exc, started_at=started_at)
            raise _http_error(exc) from exc
        _log_operation_success(
            "list_nodes",
            started_at=started_at,
            item_count=len(page.items),
            has_more=page.has_more,
        )
        return FeishuKnowledgeNodeListResponse(
            space_id=page.space_id,
            space_name=page.space_name,
            items=[
                _node_response(request_feishu_knowledge_service(request), item)
                for item in page.items
            ],
            has_more=page.has_more,
            page_token=page.page_token,
        )

    @router.post("/preview", response_model=FeishuSelectionPreviewResponse)
    async def preview_selection(
        request: Request,
        payload: FeishuSelectionPreviewRequest,
    ) -> FeishuSelectionPreviewResponse:
        started_at = perf_counter()
        try:
            result = await request_feishu_knowledge_service(request).preview(payload.node_tokens)
        except (FeishuConfigurationError, FeishuApiError, ValueError) as exc:
            _log_operation_failure("preview_selection", exc, started_at=started_at)
            raise _http_error(exc) from exc
        status_counts = Counter(item.status.value for item in result.items)
        _log_operation_success(
            "preview_selection",
            started_at=started_at,
            requested_count=len(payload.node_tokens),
            valid_count=result.valid_count,
            status_counts=dict(status_counts),
        )
        return FeishuSelectionPreviewResponse(
            space_id=result.space_id,
            valid_count=result.valid_count,
            items=[
                FeishuSelectionPreviewItemResponse(
                    **item.model_dump(mode="json"),
                )
                for item in result.items
            ],
        )

    @router.post("/imports", response_model=FeishuImportResponse)
    async def create_import(
        request: Request,
        payload: FeishuImportRequest,
    ) -> FeishuImportResponse:
        started_at = perf_counter()
        try:
            coordinator = sync_coordinator_provider(request)
            run = await coordinator.submit_import(
                node_tokens=payload.node_tokens,
                tracking_mode=payload.tracking_mode,
                schedule_enabled=payload.schedule_enabled,
                interval_seconds=payload.interval_seconds,
                request_id=request.state.request_id,
            )
            manager = upload_manager_provider(request)
            uploads = _run_uploads(run, manager)
            if not uploads and run.status == FeishuSyncRunStatus.FAILED:
                failed_item = next((item for item in run.items if item.message), None)
                message = run.error_message or (
                    failed_item.message if failed_item is not None else "飞书文档导入失败"
                )
                provider_code = run.provider_code or (
                    failed_item.provider_code if failed_item is not None else None
                )
                provider_http_status = run.provider_http_status or (
                    failed_item.provider_http_status if failed_item is not None else None
                )
                retryable = run.retryable or (
                    failed_item.retryable if failed_item is not None else False
                )
                if provider_code is not None or provider_http_status is not None or retryable:
                    raise FeishuApiError(
                        message,
                        code=provider_code,
                        http_status=provider_http_status,
                        retryable=retryable,
                    )
                raise ValueError(message)
        except (
            FeishuConfigurationError,
            FeishuApiError,
            FeishuSyncSourceBusyError,
            ValueError,
        ) as exc:
            _log_operation_failure("create_import", exc, started_at=started_at)
            raise _http_error(exc) from exc
        _log_operation_success(
            "create_import",
            started_at=started_at,
            requested_count=len(payload.node_tokens),
            accepted_count=len(uploads),
            library_id=coordinator.service.library_id(),
            source_id=run.source_id,
            run_id=run.id,
            sync_status=run.status.value,
        )
        source = coordinator.store.get_source(run.source_id)
        return FeishuImportResponse(
            space_id=coordinator.service.status().space_id,
            library_id=coordinator.service.library_id(),
            uploads=[_import_upload_response(item) for item in uploads],
            source_id=run.source_id,
            run_id=run.id,
            sync_status=run.status,
            waiting_count=sum(item.status == FeishuSyncItemStatus.WAITING for item in run.items),
            schedule_enabled=source.schedule_enabled if source else False,
            interval_seconds=source.interval_seconds if source else None,
            next_run_at=source.next_run_at if source else None,
        )

    @router.get("/imports", response_model=FeishuImportResponse)
    async def list_imports(request: Request) -> FeishuImportResponse:
        service = request_feishu_knowledge_service(request)
        uploads = service.list_imports(upload_manager_provider(request))
        space_id = service.status().space_id or ""
        library_id = service.library_id() if space_id else ""
        return FeishuImportResponse(
            space_id=space_id,
            library_id=library_id,
            uploads=[_import_upload_response(item) for item in uploads],
        )

    @router.get("/sources", response_model=FeishuSyncSourceListResponse)
    async def list_sync_sources(request: Request) -> FeishuSyncSourceListResponse:
        coordinator = sync_coordinator_provider(request)
        await coordinator.reconcile_active_runs()
        return FeishuSyncSourceListResponse(
            items=[_source_response(item) for item in coordinator.list_sources()]
        )

    @router.patch(
        "/sources/{source_id}/schedule",
        response_model=FeishuSyncSourceResponse,
    )
    async def update_sync_schedule(
        request: Request,
        source_id: str,
        payload: FeishuScheduleUpdateRequest,
    ) -> FeishuSyncSourceResponse:
        try:
            source = sync_coordinator_provider(request).update_schedule(
                source_id,
                enabled=payload.enabled,
                interval_seconds=payload.interval_seconds,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="飞书同步来源不存在") from exc
        except (ValueError, FeishuSyncSourceBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _source_response(source)

    @router.post("/sources/{source_id}/runs", response_model=FeishuSyncRunResponse)
    async def trigger_sync_run(request: Request, source_id: str) -> FeishuSyncRunResponse:
        try:
            run = await sync_coordinator_provider(request).run_source(
                source_id,
                trigger=FeishuSyncTrigger.MANUAL,
                request_id=request.state.request_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="飞书同步来源不存在") from exc
        except FeishuSyncSourceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _run_response(run)

    @router.get(
        "/sources/{source_id}/runs",
        response_model=FeishuSyncRunListResponse,
    )
    async def list_sync_runs(
        request: Request,
        source_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> FeishuSyncRunListResponse:
        coordinator = sync_coordinator_provider(request)
        try:
            runs = coordinator.list_runs(source_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="飞书同步来源不存在") from exc
        return FeishuSyncRunListResponse(items=[_run_response(item) for item in runs])

    @router.get("/runs/{run_id}", response_model=FeishuSyncRunResponse)
    async def get_sync_run(request: Request, run_id: str) -> FeishuSyncRunResponse:
        try:
            run = await sync_coordinator_provider(request).reconcile_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="飞书同步运行不存在") from exc
        return _run_response(run)

    return router


def _node_response(
    service: FeishuKnowledgeService,
    item: FeishuWikiNode,
) -> FeishuKnowledgeNodeResponse:
    selectable, reason = service.selection_support(item)
    return FeishuKnowledgeNodeResponse(
        node_token=item.node_token,
        title=item.title or "未命名文档",
        obj_type=item.obj_type,
        has_child=item.has_child,
        parent_node_token=item.parent_node_token,
        selectable=selectable,
        selection_reason=reason,
    )


def _import_upload_response(record: KnowledgeUploadRecord) -> FeishuImportUploadResponse:
    return FeishuImportUploadResponse(
        upload_id=record.id,
        library_id=record.library_id,
        asset_id=record.asset_id,
        display_name=record.display_name,
        original_filename=record.original_filename,
        size_bytes=record.size_bytes,
        status=record.status.value,
        failure_kind=record.failure_kind.value if record.failure_kind is not None else None,
        error_message=record.error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _run_uploads(
    run: FeishuSyncRun,
    manager: KnowledgeUploadManager,
) -> list[KnowledgeUploadRecord]:
    records: list[KnowledgeUploadRecord] = []
    seen: set[str] = set()
    for item in run.items:
        if not item.upload_id or item.upload_id in seen:
            continue
        record = manager.get_upload(item.upload_id)
        if record is not None:
            records.append(record)
            seen.add(record.id)
    return records


def _source_response(source: FeishuSyncSource) -> FeishuSyncSourceResponse:
    return FeishuSyncSourceResponse(
        id=source.id,
        space_id=source.space_id,
        library_id=source.library_id,
        node_tokens=source.node_tokens,
        tracking_mode=source.tracking_mode,
        schedule_enabled=source.schedule_enabled,
        interval_seconds=source.interval_seconds,
        next_run_at=source.next_run_at,
        last_started_at=source.last_started_at,
        last_completed_at=source.last_completed_at,
    )


def _run_response(run: FeishuSyncRun) -> FeishuSyncRunResponse:
    return FeishuSyncRunResponse(
        id=run.id,
        source_id=run.source_id,
        trigger=run.trigger,
        status=run.status,
        stage=run.stage.value,
        items=[
            FeishuSyncRunItemResponse.model_validate(item.model_dump(exclude={"delete_requested"}))
            for item in run.items
        ],
        error_message=run.error_message,
        provider_code=run.provider_code,
        provider_http_status=run.provider_http_status,
        retryable=run.retryable,
        started_at=run.started_at,
        completed_at=run.completed_at,
        updated_at=run.updated_at,
    )


def request_feishu_knowledge_service(request: Request) -> FeishuKnowledgeService:
    service = getattr(request.app.state, "feishu_knowledge_service", None)
    if isinstance(service, FeishuKnowledgeService):
        return service
    config = request.app.state.config.knowledge
    if config is None:
        raise HTTPException(status_code=503, detail="knowledge backend is not configured")
    service = build_feishu_knowledge_service(config.feishu)
    request.app.state.feishu_knowledge_service = service
    return service


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FeishuConfigurationError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, FeishuSyncSourceBusyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FeishuApiError):
        if exc.permission_denied:
            return HTTPException(status_code=403, detail="应用无权访问该飞书知识库")
        if exc.not_found:
            return HTTPException(status_code=404, detail="飞书知识库节点不存在")
        return HTTPException(
            status_code=503 if exc.retryable else 502,
            detail="飞书知识库暂时不可用",
        )
    return HTTPException(status_code=500, detail="unexpected Feishu knowledge error")


def _log_operation_success(operation: str, *, started_at: float, **fields: object) -> None:
    duration_ms = int((perf_counter() - started_at) * 1000)
    logger.bind(
        source_name="knowledge.feishu",
        operation=operation,
        result="success",
        duration_ms=duration_ms,
        **fields,
    ).info("Feishu knowledge operation succeeded: {} ({}ms)", operation, duration_ms)


def _log_operation_failure(operation: str, exc: Exception, *, started_at: float) -> None:
    duration_ms = int((perf_counter() - started_at) * 1000)
    fields: dict[str, object] = {
        "source_name": "knowledge.feishu",
        "operation": operation,
        "result": "failed",
        "duration_ms": duration_ms,
        "error_class": type(exc).__name__,
    }
    if isinstance(exc, FeishuApiError):
        fields.update(
            provider_code=exc.code,
            provider_http_status=exc.http_status,
            retryable=exc.retryable,
            permission_denied=exc.permission_denied,
            not_found=exc.not_found,
            cause_class=type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
        )
    logger.bind(**fields).warning(
        "Feishu knowledge operation failed: {} (class={} duration={}ms)",
        operation,
        type(exc).__name__,
        duration_ms,
    )


__all__ = ["create_feishu_knowledge_router", "request_feishu_knowledge_service"]
