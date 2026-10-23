"""Admin operations API (auth + scheduler metrics)."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response

from viknow.api.admin_contracts import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminLogoutResponse,
    IndexJobDetailResponse,
    IndexJobRowResponse,
    IndexJobsResponse,
    JobProgressResponse,
    JobStageResponse,
    MultiAgentRuntimeResponse,
    MultiAgentRuntimeUpdateRequest,
    PoolCapacityItem,
    PoolGaugeResponse,
    ProfileGaugeResponse,
    RunStatsResponse,
    SchedulerCurrentResponse,
    SchedulerHistoryResponse,
    SchedulerPoolsResponse,
    SchedulerPoolsUpdateRequest,
    SchedulerSnapshotResponse,
    ToolStatsResponse,
)
from viknow.core.config import AppConfig
from viknow.core.knowledge.job_progress import HyperRAGJobProgressReader
from viknow.core.scheduler.config import (
    default_resource_scheduler_config,
    resource_scheduler_config_with_overrides,
)
from viknow.core.scheduler.metrics_domain import (
    MetricsSnapshot,
    PoolGauge,
    ProfileGauge,
    SchedulerWindowStats,
    pool_gauge_from_snapshot,
    utc_now,
)
from viknow.core.scheduler.metrics_store import RETENTION_DAYS, SchedulerMetricsStore
from viknow.core.scheduler.resource_scheduler import ResourceScheduler

ADMIN_COOKIE_NAME = "viknow_admin_token"
ADMIN_SESSION_TTL_SECONDS = 24 * 60 * 60
CURRENT_STATS_WINDOW = timedelta(hours=1)

_admin_sessions: dict[str, float] = {}


def _extract_token(
    *,
    authorization: str | None,
    cookie_token: str | None,
) -> str | None:
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if cookie_token is not None and cookie_token.strip():
        return cookie_token.strip()
    return None


def _optional_admin_token(
    authorization: Annotated[str | None, Header()] = None,
    cookie_token: Annotated[str | None, Cookie(alias=ADMIN_COOKIE_NAME)] = None,
) -> str | None:
    return _extract_token(authorization=authorization, cookie_token=cookie_token)


def _create_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time() + ADMIN_SESSION_TTL_SECONDS
    return token


def _session_valid(token: str) -> bool:
    expires_at = _admin_sessions.get(token)
    if expires_at is None:
        return False
    if expires_at < time.time():
        _admin_sessions.pop(token, None)
        return False
    return True


def require_admin_auth(
    token: Annotated[str | None, Depends(_optional_admin_token)],
) -> str:
    if token is None or not _session_valid(token):
        raise HTTPException(status_code=401, detail="admin authentication required")
    return token


def _yaml_multi_agent_enabled(config: AppConfig) -> bool:
    return bool(config.agent.deepagents.multi_agent.enabled)


def _sidecar_profile_ids(config: AppConfig, catalog: object | None) -> list[str]:
    if catalog is None:
        return []
    from viknow.core.subagents import load_profile_subagents

    return [
        profile.id
        for profile in catalog.all()
        if load_profile_subagents(profile.id, config.agent.profile_dir)
    ]


def _multi_agent_runtime_status(request: Request) -> MultiAgentRuntimeResponse:
    config: AppConfig = request.app.state.config
    yaml_enabled = _yaml_multi_agent_enabled(config)
    override = getattr(request.app.state, "multi_agent_enabled_override", None)
    return MultiAgentRuntimeResponse(
        enabled=yaml_enabled if override is None else bool(override),
        yaml_enabled=yaml_enabled,
        overridden=override is not None,
        sidecar_profiles=_sidecar_profile_ids(
            config, getattr(request.app.state, "profile_catalog", None)
        ),
    )


def _validate_live_subagent_sidecars(request: Request) -> None:
    config: AppConfig = request.app.state.config
    catalog = getattr(request.app.state, "profile_catalog", None)
    if catalog is None:
        return
    from viknow.core.monitoring import MONITORING_TOOL_IDS
    from viknow.core.subagents import load_profile_subagents, validate_profile_subagents
    from viknow.core.tools import build_viknow_tool_specs

    supported = {spec.id for spec in build_viknow_tool_specs()} | set(MONITORING_TOOL_IDS)
    for profile in catalog.all():
        validate_profile_subagents(
            load_profile_subagents(profile.id, config.agent.profile_dir),
            profile_id=profile.id,
            supported_tools=supported,
            skill_dirs=config.agent.skill_dirs,
        )


def create_admin_router() -> APIRouter:
    """Build the Admin API router (mounted via ``include_router``)."""

    router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

    @router.post("/login", response_model=AdminLoginResponse)
    def login(
        payload: AdminLoginRequest, request: Request, response: Response
    ) -> AdminLoginResponse:
        admin = request.app.state.admin_config
        if not admin.enabled:
            raise HTTPException(status_code=503, detail="admin credentials are not configured")
        if payload.username != admin.username or payload.password != admin.password:
            raise HTTPException(status_code=401, detail="invalid admin credentials")
        token = _create_admin_session()
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=ADMIN_SESSION_TTL_SECONDS,
        )
        return AdminLoginResponse(ok=True, token=token)

    @router.post("/config/reload")
    async def reload_runtime_config(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> dict[str, object]:
        from viknow.core.config_reload import (
            ConfigFoundationChanged,
            ConfigReloadError,
            apply_config_reload,
        )

        try:
            config = await apply_config_reload(request.app)
        except ConfigFoundationChanged as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConfigReloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "log_level": config.app.log_level,
            "config_path": str(config._config_path),
        }

    @router.get("/runtime/multi-agent", response_model=MultiAgentRuntimeResponse)
    def multi_agent_get(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> MultiAgentRuntimeResponse:
        return _multi_agent_runtime_status(request)

    @router.put("/runtime/multi-agent", response_model=MultiAgentRuntimeResponse)
    def multi_agent_put(
        request: Request,
        payload: MultiAgentRuntimeUpdateRequest,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> MultiAgentRuntimeResponse:
        current = _multi_agent_runtime_status(request)
        if payload.enabled and not current.enabled:
            try:
                _validate_live_subagent_sidecars(request)
            except RuntimeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.multi_agent_enabled_override = payload.enabled
        if current.enabled != payload.enabled:
            registry = getattr(request.app.state, "agent_runtime_registry", None)
            catalog = getattr(request.app.state, "profile_catalog", None)
            if registry is not None and catalog is not None:
                registry.reset_profile_cache(catalog)
        return _multi_agent_runtime_status(request)

    @router.post("/logout", response_model=AdminLogoutResponse)
    def logout(
        response: Response,
        token: Annotated[str | None, Depends(_optional_admin_token)] = None,
    ) -> AdminLogoutResponse:
        if token is not None:
            _admin_sessions.pop(token, None)
        response.delete_cookie(ADMIN_COOKIE_NAME)
        return AdminLogoutResponse(ok=True)

    @router.get("/scheduler/current", response_model=SchedulerCurrentResponse)
    def scheduler_current(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
        profile_id: str | None = Query(default=None),
    ) -> SchedulerCurrentResponse:
        store = _require_metrics_store(request)
        snapshot = _current_snapshot(request, profile_id=profile_id)
        ended = utc_now()
        started = ended - CURRENT_STATS_WINDOW
        stats = store.window_stats(
            started_at=started,
            ended_at=ended,
            profile_id=profile_id,
        )
        return SchedulerCurrentResponse(
            snapshot=_snapshot_response(snapshot) if snapshot is not None else None,
            runs=_run_stats_response(stats),
            tools=_tool_stats_response(stats),
        )

    @router.get("/scheduler/history", response_model=SchedulerHistoryResponse)
    def scheduler_history(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
        from_ts: Annotated[datetime, Query(alias="from")],
        to_ts: Annotated[datetime, Query(alias="to")],
        profile_id: Annotated[str | None, Query()] = None,
    ) -> SchedulerHistoryResponse:
        store = _require_metrics_store(request)
        _validate_history_window(from_ts, to_ts)
        snapshots = store.list_snapshots(
            started_at=from_ts,
            ended_at=to_ts,
            profile_id=profile_id,
        )
        stats = store.window_stats(
            started_at=from_ts,
            ended_at=to_ts,
            profile_id=profile_id,
        )
        return SchedulerHistoryResponse.model_validate(
            {
                "from": from_ts,
                "to": to_ts,
                "snapshots": [_snapshot_response(item) for item in snapshots],
                "runs": _run_stats_response(stats).model_dump(),
                "tools": _tool_stats_response(stats).model_dump(),
            }
        )

    @router.get("/scheduler/pools", response_model=SchedulerPoolsResponse)
    def scheduler_pools_get(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> SchedulerPoolsResponse:
        return SchedulerPoolsResponse(pools=_effective_pool_capacities(request))

    @router.put("/scheduler/pools", response_model=SchedulerPoolsResponse)
    def scheduler_pools_put(
        request: Request,
        payload: SchedulerPoolsUpdateRequest,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> SchedulerPoolsResponse:
        defaults = default_resource_scheduler_config().resources
        overrides = dict(getattr(request.app.state, "scheduler_pool_overrides", {}) or {})
        for item in payload.pools:
            if item.resource not in defaults:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown resource pool: {item.resource}",
                )
            overrides[item.resource] = item.capacity
        request.app.state.scheduler_pool_overrides = overrides

        errors: list[str] = []
        for profile_id, scheduler in _iter_live_schedulers(request):
            for item in payload.pools:
                try:
                    scheduler.update_pool_capacity(item.resource, item.capacity)
                except KeyError:
                    errors.append(f"{profile_id}:{item.resource}")
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        if errors:
            raise HTTPException(
                status_code=400,
                detail=f"unknown resource pool on live scheduler: {', '.join(errors)}",
            )
        return SchedulerPoolsResponse(pools=_effective_pool_capacities(request))

    @router.get("/jobs", response_model=IndexJobsResponse)
    def index_jobs(
        request: Request,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> IndexJobsResponse:
        """任务作业列表：上传记录（含 job_id）与 HyperRAG 分阶段进度合并。"""
        records = _upload_records(request)
        reader = _job_progress_reader(request)
        rows = [
            _index_job_row(record, reader.get_job(record.job_id) if record.job_id else None)
            for record in records
        ]
        return IndexJobsResponse(jobs=rows)

    @router.get("/jobs/{job_id}", response_model=IndexJobDetailResponse)
    def index_job_detail(
        request: Request,
        job_id: str,
        _: Annotated[str, Depends(require_admin_auth)],
    ) -> IndexJobDetailResponse:
        """任务作业详情：job 分阶段进度（阶段 stepper 数据源）。"""
        reader = _job_progress_reader(request)
        progress = reader.get_job(job_id)
        if progress is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        record = _upload_record_by_job(request, job_id)
        return IndexJobDetailResponse(
            upload_id=record.id if record is not None else None,
            library_id=progress.workspace,
            asset_id=progress.asset_id,
            display_name=record.display_name if record is not None else None,
            original_filename=record.original_filename if record is not None else None,
            size_bytes=record.size_bytes if record is not None else None,
            created_at=record.created_at if record is not None else None,
            updated_at=record.updated_at if record is not None else None,
            job_id=job_id,
            job=_job_progress_response(progress),
        )

    return router


def _require_metrics_store(request: Request) -> SchedulerMetricsStore:
    store = getattr(request.app.state, "scheduler_metrics_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="scheduler metrics store is unavailable")
    return store


def _validate_history_window(from_ts: datetime, to_ts: datetime) -> None:
    if to_ts < from_ts:
        raise HTTPException(status_code=400, detail="'to' must be greater than or equal to 'from'")
    now = utc_now()
    retention_start = now - timedelta(days=RETENTION_DAYS)
    from_cmp = from_ts if from_ts.tzinfo is not None else from_ts.replace(tzinfo=now.tzinfo)
    if from_cmp < retention_start:
        raise HTTPException(
            status_code=400,
            detail=f"history window exceeds {RETENTION_DAYS}-day retention",
        )
    if (to_ts - from_ts) > timedelta(days=RETENTION_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"history span exceeds {RETENTION_DAYS}-day retention",
        )


def _snapshot_response(snapshot: MetricsSnapshot) -> SchedulerSnapshotResponse:
    return SchedulerSnapshotResponse(
        ts=snapshot.ts,
        active_runs=snapshot.active_runs,
        load=snapshot.load,
        scheduler_mode=snapshot.scheduler_mode,
        profiles=[
            ProfileGaugeResponse(
                profile_id=profile.profile_id,
                active_runs=profile.active_runs,
                load=profile.load,
                pools=[
                    PoolGaugeResponse(
                        resource=pool.resource,
                        capacity=pool.capacity,
                        in_use=pool.in_use,
                        queued=pool.queued,
                        in_use_interactive=pool.in_use_interactive,
                        in_use_background=pool.in_use_background,
                        queue_key=pool.queue_key,
                        job_capacity=pool.job_capacity,
                        tool_in_use=pool.tool_in_use,
                        job_in_use=pool.job_in_use,
                        queued_tool=pool.queued_tool,
                        queued_job=pool.queued_job,
                    )
                    for pool in profile.pools
                ],
            )
            for profile in snapshot.profiles
        ],
    )


def _run_stats_response(stats: SchedulerWindowStats) -> RunStatsResponse:
    return RunStatsResponse(
        run_count=stats.runs.run_count,
        avg_duration_ms=stats.runs.avg_duration_ms,
        p50_duration_ms=stats.runs.p50_duration_ms,
        p95_duration_ms=stats.runs.p95_duration_ms,
    )


def _tool_stats_response(stats: SchedulerWindowStats) -> ToolStatsResponse:
    return ToolStatsResponse(
        timeout_count=stats.tools.timeout_count,
        failure_count=stats.tools.failure_count,
        by_tool=stats.tools.by_tool,
    )


def _effective_pool_capacities(request: Request) -> list[PoolCapacityItem]:
    overrides = getattr(request.app.state, "scheduler_pool_overrides", None) or {}
    for _, scheduler in _iter_live_schedulers(request):
        return [
            PoolCapacityItem(resource=resource, capacity=capacity)
            for resource, capacity in sorted(scheduler.pool_configs().items())
        ]
    config = getattr(request.app.state, "config", None)
    if isinstance(config, AppConfig) and config.scheduler.unified.enabled:
        scheduler_config = config.scheduler.unified.to_scheduler_config()
        return [
            PoolCapacityItem(resource=domain, capacity=cfg.capacity)
            for domain, cfg in sorted(scheduler_config.domains.items())
        ]
    config = resource_scheduler_config_with_overrides(overrides)
    return [
        PoolCapacityItem(resource=resource, capacity=pool.capacity)
        for resource, pool in sorted(config.resources.items())
    ]


def _current_snapshot(
    request: Request,
    *,
    profile_id: str | None,
) -> MetricsSnapshot | None:
    """Prefer live scheduler gauges; fall back to idle defaults (not stale DB rows)."""

    live = _live_snapshot(request, profile_id=profile_id)
    if live is not None:
        return live
    return _idle_default_snapshot(request)


def _live_snapshot(
    request: Request,
    *,
    profile_id: str | None,
) -> MetricsSnapshot | None:
    profiles: list[ProfileGauge] = []
    for pid, scheduler in _iter_live_schedulers(request):
        if profile_id is not None and pid != profile_id:
            continue
        profiles.append(
            ProfileGauge(
                profile_id=pid,
                active_runs=scheduler.active_runs,
                load=scheduler.load,
                pools=[pool_gauge_from_snapshot(item) for item in scheduler.pool_snapshots()],
            )
        )
    if not profiles:
        return None
    live_modes = {sched.scheduler_mode for _, sched in _iter_live_schedulers(request)}
    mode = "unified" if "unified" in live_modes else "legacy"
    snapshot = MetricsSnapshot.from_profile_gauges(ts=utc_now(), profiles=profiles)
    return snapshot.model_copy(update={"scheduler_mode": mode})


def _idle_default_snapshot(request: Request) -> MetricsSnapshot:
    config = getattr(request.app.state, "config", None)
    if isinstance(config, AppConfig) and config.scheduler.unified.enabled:
        from viknow.core.scheduler.unified_metrics import idle_pool_snapshots_from_config

        scheduler_config = config.scheduler.unified.to_scheduler_config()
        pools = [
            pool_gauge_from_snapshot(item)
            for item in idle_pool_snapshots_from_config(scheduler_config)
        ]
        profile = ProfileGauge(
            profile_id="unified",
            active_runs=0,
            load=0.0,
            pools=pools,
        )
        return MetricsSnapshot.from_profile_gauges(
            ts=utc_now(),
            profiles=[profile],
        ).model_copy(update={"scheduler_mode": "unified"})

    overrides = getattr(request.app.state, "scheduler_pool_overrides", None) or {}
    config = resource_scheduler_config_with_overrides(overrides)
    pools = [
        PoolGauge(
            resource=resource,
            capacity=pool.capacity,
            in_use=0,
            queued=0,
            in_use_interactive=0,
            in_use_background=0,
        )
        for resource, pool in sorted(config.resources.items())
    ]
    profile = ProfileGauge(
        profile_id="defaults",
        active_runs=0,
        load=0.0,
        pools=pools,
    )
    return MetricsSnapshot.from_profile_gauges(ts=utc_now(), profiles=[profile]).model_copy(
        update={"scheduler_mode": "legacy"}
    )


def _iter_live_schedulers(request: Request) -> list[tuple[str, ResourceScheduler]]:
    registry = getattr(request.app.state, "agent_runtime_registry", None)
    if registry is None:
        return []
    items: list[tuple[str, ResourceScheduler]] = []
    for profile_id, runtime in registry.cached_runtimes().items():
        scheduler = getattr(runtime, "scheduler", None)
        if isinstance(scheduler, ResourceScheduler) and scheduler.scheduler_mode == "unified":
            items.append((profile_id, scheduler))
    return items


def _job_progress_reader(request: Request) -> HyperRAGJobProgressReader:
    reader = getattr(request.app.state, "job_progress_reader", None)
    if reader is None:
        config = getattr(request.app.state, "config", None)
        if not isinstance(config, AppConfig):
            raise HTTPException(status_code=503, detail="app config is not initialized")
        reader = HyperRAGJobProgressReader(config)
        request.app.state.job_progress_reader = reader
    return reader


def _upload_records(request: Request) -> list:
    """读取知识库上传记录（含持久化的 job_id）。knowledge 未配置时返回空列表。"""
    manager = getattr(request.app.state, "knowledge_upload_manager", None)
    if manager is None:
        return []
    try:
        return manager.list_uploads()
    except Exception:
        # knowledge backend 不可用时不阻塞 admin 其它页签
        return []


def _upload_record_by_job(request: Request, job_id: str):
    for record in _upload_records(request):
        if record.job_id == job_id:
            return record
    return None


def _datetime_from_epoch(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _duration_seconds(started_at: float | None, completed_at: float | None) -> float | None:
    if started_at is None:
        return None
    end = completed_at if completed_at is not None else time.time()
    if end < started_at:
        return None
    return round(end - started_at, 3)


def _job_stage_response(stage) -> JobStageResponse:
    return JobStageResponse(
        name=stage.name,
        status=stage.status,
        task_id=stage.task_id,
        started_at=_datetime_from_epoch(stage.started_at),
        completed_at=_datetime_from_epoch(stage.completed_at),
        error_message=stage.error_message,
        duration_seconds=_duration_seconds(stage.started_at, stage.completed_at),
    )


def _job_progress_response(progress) -> JobProgressResponse:
    return JobProgressResponse(
        job_id=progress.job_id,
        job_name=progress.job_name,
        workspace=progress.workspace,
        asset_id=progress.asset_id,
        status=progress.status,
        overall_progress=round(progress.overall_progress, 4),
        stage_count=progress.stage_count,
        completed_stage_count=progress.completed_stage_count,
        stages=[_job_stage_response(stage) for stage in progress.stages],
        created_at=_datetime_from_epoch(progress.created_at),
        completed_at=_datetime_from_epoch(progress.completed_at),
        error_message=progress.error_message,
        running_duration_seconds=progress.running_duration_seconds,
    )


def _index_job_row(record, progress) -> IndexJobRowResponse:
    return IndexJobRowResponse(
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
        job_id=record.job_id,
        job=_job_progress_response(progress) if progress is not None else None,
    )


__all__ = [
    "ADMIN_COOKIE_NAME",
    "create_admin_router",
    "require_admin_auth",
]
