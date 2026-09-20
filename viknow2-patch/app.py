"""FastAPI application for the minimal ViKnow QA UI."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from PIL import Image
from pydantic import ValidationError

from viknow.agent_adapters.deepagents_config import resolve_deepagents_config
from viknow.api.admin import create_admin_router
from viknow.api.chapters import create_chapter_router
from viknow.api.coach import create_coach_router
from viknow.api.contracts import (
    AgentModelsResponse,
    AgentProfileOptionResponse,
    AgentProfilesResponse,
    CreateKnowledgeIndexRequest,
    CreateKnowledgeIndexResponse,
    CreateRunRequest,
    CreateRunResponse,
    DeleteKnowledgeIndexRequest,
    DeleteKnowledgeIndexResponse,
    DeleteKnowledgeUploadResponse,
    EvalRunRequest,
    EvalRunResponse,
    ExportKnowledgePackageRequest,
    ExportKnowledgePackageResponse,
    ImportKnowledgePackageRequest,
    ImportKnowledgePackageResponse,
    InspectToolEvalRejudgeCaseRequest,
    InspectToolEvalRejudgeCaseResponse,
    InspectToolEvalRunCaseRequest,
    InspectToolEvalRunCaseResponse,
    KnowledgeAssetIndexResponse,
    KnowledgeIndexItemResponse,
    KnowledgeIndexJobResponse,
    KnowledgeSearchRequest,
    KnowledgeUploadListResponse,
    KnowledgeUploadResponse,
    KnowledgeUploadVideoSummaryResponse,
    LearningFeedbackResponse,
    MigrationExportFile,
    MigrationExportListResponse,
    MigrationExportRequest,
    MigrationImportFromExportRequest,
    MigrationJobAcceptedResponse,
    MigrationJobResponse,
    SessionFileListResponse,
    SessionFileResponse,
    SessionResponse,
    SubmitLearningFeedbackRequest,
    SubmitRequest,
    UpdateKnowledgeUploadVideoSummaryRequest,
    UpdateKnowledgeUploadVideoSummaryResponse,
    UserResponse,
)
from viknow.api.feishu_knowledge import create_feishu_knowledge_router
from viknow.api.vicoach import create_vicoach_router
from viknow.capabilities.vicoach.chapters.jobs import ChapterJobs
from viknow.capabilities.vicoach.chapters.published_reader import PublishedChapterReader
from viknow.capabilities.vicoach.chapters.service import ChapterService
from viknow.capabilities.vicoach.courses import CourseService
from viknow.capabilities.vicoach.factory import build_vicoach_store
from viknow.capabilities.vicoach.models import (
    StructuredLLMFreeTextAnswerJudge,
    StructuredLLMQuestionGenerator,
)
from viknow.capabilities.vicoach.service import VicoachService
from viknow.capabilities.vicoach.training import VicoachTrainingService
from viknow.core.config import (
    AppConfig,
    ModelProvider,
    PersistenceBackend,
    load_app_config,
    load_tool_configs,
    materialize_runtime_tool_configs,
    resolve_files_root,
    resolve_model_config,
    resolve_scheduler_postgres_dsn,
)
from viknow.core.config_reload import (
    register_config_reload_signal,
    start_config_reload_watcher,
    stop_config_reload_watcher,
)
from viknow.core.env import load_local_env
from viknow.core.eval import EvalRunTimeout, EvalService
from viknow.core.eval.inspect_tool import (
    InspectToolEvalCase,
    InspectToolEvalService,
    build_inspect_tool_judge,
)
from viknow.core.filesystem.manager import FilesystemManager
from viknow.core.knowledge.feishu import build_feishu_knowledge_service
from viknow.core.knowledge.feishu.executor import FeishuSyncCoordinator
from viknow.core.knowledge.feishu.factory import build_feishu_sync_store
from viknow.core.knowledge.feishu.schedule import FeishuSyncScheduler
from viknow.core.knowledge.hyperrag_adapter import HyperRAGAdapter, build_hyperrag_adapter
from viknow.core.knowledge.job_progress import HyperRAGJobProgressReader
from viknow.core.knowledge.migration import (
    KnowledgeMigrationService,
    MigrationJob,
    build_migration_service,
)
from viknow.core.knowledge.uploads import KnowledgeUploadManager, KnowledgeUploadRecord
from viknow.core.knowledge.uploads.factory import build_knowledge_upload_store
from viknow.core.knowledge.video_indexing.visualization import IndexVisualizationService
from viknow.core.learning.factory import build_learning_projection_store
from viknow.core.learning.service import LearningProjectionService
from viknow.core.learning.worker import start_learning_projection_worker
from viknow.core.logging import configure_logging, logging_context
from viknow.core.memory import UserMemoryService, UserPreferences, build_user_memory_service
from viknow.core.multimodal.locate_anything import ActioLocateAnythingClient
from viknow.core.observability.otel import setup_otel, shutdown_otel
from viknow.core.profiles import (
    AgentProfileCatalog,
    UnknownProfile,
    profile_capability_signatures,
    validate_profile_catalog,
)
from viknow.core.runtime.builder import build_agent_runtime, resolve_session_workspace_dir
from viknow.core.runtime.domain import (
    AgentEvent,
    AgentEventType,
    AgentOp,
    AgentOpType,
    AgentProfile,
    AgentStatus,
    AgentStatusType,
    SubmitResult,
)
from viknow.core.runtime.events import AgentRuntime
from viknow.core.runtime.registry import AgentRuntimeRegistry
from viknow.core.scheduler.domain import Priority, extra_body_with_priority
from viknow.core.session import (
    RunSubmissionFailed,
    SessionBusy,
    SessionDebug,
    SessionNotFound,
    SessionProfileConflict,
    SessionProfileUnavailable,
    UserNotFound,
)
from viknow.core.session.domain import (
    InvalidAttachmentFiles,
    RunNotFound,
    RunRecord,
    RunStatus,
    SessionFileNotFound,
    SessionFileTooLarge,
    ToolCallNotFound,
)
from viknow.core.session.events import SessionEventStore
from viknow.core.session.factory import build_session_store
from viknow.core.session.leader_service import (
    acquire_leader_lease,
    assert_configured_redis_db0_allows_only_leader,
    is_leader_service,
    release_leader_lease,
    start_leader_lease_renewal,
)
from viknow.core.session.manager import SessionManager
from viknow.core.session.store import SessionStore
from viknow.core.session.timeline import (
    AnswerReviewDetail,
    ReasoningDetail,
    SessionTimeline,
    ToolCallDetail,
)
from viknow.core.tools.executor import configure_tool_thread_pool
from viknow.core.tools.knowledge import KnowledgeToolHandlers
from viknow.deployment.license import LicenseError, LicenseGuard, resolve_license_config

_ACCESS_LOG_QUIET_GET_PREFIXES = (
    "/api/v1/knowledge/uploads",
    "/api/v1/knowledge/feishu/imports",
)


def _quiet_access_log(method: str, path: str, status_code: int) -> bool:
    """Dev polling endpoints: keep successful GETs at DEBUG to avoid log spam."""
    if method != "GET" or status_code >= 400:
        return False
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in _ACCESS_LOG_QUIET_GET_PREFIXES
    )


load_local_env()


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or get_app_config()
    tool_configs = load_tool_configs(app_config)
    validate_profile_catalog(app_config, app_config.profile_catalog, tool_configs)
    configure_logging(app_config)

    from viknow.core.observability.langfuse_project_identity import LangfuseProjectIdentity

    langfuse_credentials = [
        os.getenv(name)
        for name in (
            "VIKNOW_LANGFUSE_HOST",
            "VIKNOW_LANGFUSE_PUBLIC_KEY",
            "VIKNOW_LANGFUSE_SECRET_KEY",
        )
    ]
    langfuse_project_identity = (
        LangfuseProjectIdentity(
            host=langfuse_credentials[0],
            public_key=langfuse_credentials[1],
            secret_key=langfuse_credentials[2],
        )
        if all(langfuse_credentials)
        else None
    )

    license_config = resolve_license_config(app_config.license)
    license_guard = LicenseGuard(license_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # mem0 与 runtime 在 lifespan 内构建：只有真正提供服务的 worker 进程会执行
        # lifespan。uvicorn --reload 的 reloader 进程也会 import 本模块（解析 `app`
        # 对象）从而执行 create_app()，但不会运行 lifespan。若在 create_app 顶层
        # 构建 mem0，reloader 会打开 Qdrant 本地文件锁，导致 worker 的 mem0 初始化
        # 因 "already accessed by another instance" 失败并静默降级为 NoOp。
        #
        # Private deployment: license verification runs before mem0/runtime init.
        # Development keeps license.enabled=false and skips this path entirely.
        app.state.license_guard.verify_startup()
        configure_tool_thread_pool(max_workers=app_config.share.tools.thread_pool_size)
        # Redis db0 TTL lease decides leadership (reclaim + exclusive db0).
        # Crash without DEL is fine: key TTL expires and the next db0 process
        # may acquire. If this process loses the lease while alive, renewal
        # fences by SIGTERM so it cannot stay a demoted dual writer.
        acquire_leader_lease(app_config)
        lease_renew_task = start_leader_lease_renewal()
        assert_configured_redis_db0_allows_only_leader(app_config)
        register_config_reload_signal(app)
        await start_config_reload_watcher(app)
        app.state.license_locked = False

        session_store = app.state.session_store
        knowledge_adapter = app.state.knowledge_adapter
        from viknow.core.answer_review.replay import build_answer_review_replayer
        from viknow.core.learning.case_review_factory import build_case_review_store
        from viknow.core.learning.case_review_service import CaseReviewService
        from viknow.core.learning.proposal_analysis import build_run_trace_proposal_analyzer

        case_review_store = build_case_review_store(app_config)

        def current_answer_review_model_config():
            current_tool_configs = app.state.tool_configs
            current_config = app.state.config
            if current_tool_configs.multimodal is not None:
                return current_tool_configs.multimodal.review_model
            return resolve_model_config(
                current_config.agent.model,
                current_config.model_providers,
                field_name="agent.model",
            )

        app.state.case_review_service = CaseReviewService(
            session_store=session_store,
            review_store=case_review_store,
            proposal_analyzer=build_run_trace_proposal_analyzer(
                app_config,
                app.state.tool_configs,
            )
            if app_config.self_learning_analysis.enabled
            else None,
            answer_review_replayer=build_answer_review_replayer(current_answer_review_model_config),
            target_skill_id=app_config.self_learning_analysis.target_skill_id,
            skillopt_experiment_root=app_config.self_learning_skill_optimization.experiment_root,
        )
        if (
            isinstance(knowledge_adapter, HyperRAGAdapter)
            and app_config.knowledge is not None
            and app_config.knowledge.hyperrag.should_eager_start()
        ):
            await knowledge_adapter.start_background_runtime()
        await app.state.learning_chapters.recover()
        await app.state.coach_courses.recover()
        app.state.tool_configs = materialize_runtime_tool_configs(
            app_config,
            app.state.tool_configs,
        )
        app.state.runtime_tool_configs_ready = True
        app.state.user_memory_service = build_user_memory_service(app_config)
        learning_service = _build_learning_projection_service(app_config, session_store)
        app.state.learning_projection_service = learning_service
        app.state.session_event_store = SessionEventStore(
            session_store,
            on_terminal=(
                learning_service.enqueue_terminal_event
                if learning_service is not None and app_config.learning.auto_project_completed_runs
                else None
            ),
        )
        _reclaim_stale_session_runs_if_leader(session_store)

        async def _on_license_failure(exc: LicenseError) -> None:
            app.state.license_locked = True
            logger.error("license invalid during runtime; locking service: {}", exc)
            # Give in-flight handlers a moment, then hard-stop the process.
            await asyncio.sleep(0.1)
            raise SystemExit(f"license invalid: {exc}")

        revalidate_task = app.state.license_guard.start_periodic_revalidation(
            on_failure=_on_license_failure
        )
        from viknow.core.scheduler.unified_reconcile import (
            reconcile_unified_scheduler_state,
            run_orphan_reconcile_loop,
        )

        orphan_stop = asyncio.Event()
        orphan_task: asyncio.Task[None] | None = None
        if is_leader_service():
            reconcile_unified_scheduler_state(app_config)
            if app_config.scheduler.unified.enabled:
                orphan_task = asyncio.create_task(
                    run_orphan_reconcile_loop(app_config, stop=orphan_stop)
                )
        else:
            logger.warning("Skipping unified scheduler Redis reconcile; leader lease not held")
        metrics_collector = await _start_scheduler_metrics(app, app_config)
        usage_push_worker = _start_usage_push_worker(app_config)
        feishu_sync_scheduler = None
        feishu_service = getattr(app.state, "feishu_knowledge_service", None)
        if feishu_service is not None and feishu_service.status().configured:
            coordinator = _ensure_feishu_sync_coordinator(app)
            feishu_sync_scheduler = FeishuSyncScheduler(
                coordinator,
                poll_interval_seconds=app_config.knowledge.feishu.sync_poll_interval_seconds,
            )
            app.state.feishu_sync_scheduler = feishu_sync_scheduler
            feishu_sync_scheduler.start()
        learning_projection_worker = start_learning_projection_worker(
            learning_service,
            app_config.learning,
        )
        from viknow.core.learning.proposal_worker import start_optimization_analysis_worker

        optimization_analysis_worker = start_optimization_analysis_worker(
            app.state.case_review_service,
            app_config.self_learning_analysis,
        )
        from viknow.core.learning.oracle_worker import start_oracle_worker

        oracle_worker = start_oracle_worker(
            review_service=app.state.case_review_service,
            store=case_review_store,
            eval_service=EvalService(
                session_manager=SessionManager(
                    store=session_store,
                    catalog=app.state.profile_catalog,
                    runtime_registry=app.state.agent_runtime_registry,
                    filesystem_manager=getattr(app.state, "filesystem_manager", None),
                ),
                store=session_store,
                knowledge_adapter=knowledge_adapter,
            ),
            revision_resolver=KnowledgeToolHandlers(knowledge_adapter=knowledge_adapter),
            config=app_config.self_learning_oracle,
        )
        from viknow.core.learning.skill_optimization_worker import (
            start_skill_optimization_worker,
        )

        skill_optimization_worker = start_skill_optimization_worker(
            review_service=app.state.case_review_service,
            store=case_review_store,
            config=app_config.self_learning_skill_optimization,
            app_config=app_config,
        )
        identity_stop = asyncio.Event()
        identity_task = (
            asyncio.create_task(langfuse_project_identity.run(identity_stop))
            if langfuse_project_identity is not None
            else None
        )
        try:
            yield
        finally:
            identity_stop.set()
            if identity_task is not None:
                await identity_task
            orphan_stop.set()
            if orphan_task is not None:
                orphan_task.cancel()
                with suppress(asyncio.CancelledError):
                    await orphan_task
            if lease_renew_task is not None:
                lease_renew_task.cancel()
                with suppress(asyncio.CancelledError):
                    await lease_renew_task
            release_leader_lease()
            if feishu_sync_scheduler is not None:
                await feishu_sync_scheduler.stop()
            await stop_config_reload_watcher(app)
            if usage_push_worker is not None:
                await usage_push_worker.stop()
            if learning_projection_worker is not None:
                await learning_projection_worker.stop()
            if optimization_analysis_worker is not None:
                await optimization_analysis_worker.stop()
            if oracle_worker is not None:
                await oracle_worker.stop()
            if skill_optimization_worker is not None:
                await skill_optimization_worker.stop()
            if metrics_collector is not None:
                await metrics_collector.stop()
            from viknow.core.postgres_pool import close_all_pools
            from viknow.core.scheduler.metrics_events import bind_scheduler_metrics_store

            bind_scheduler_metrics_store(None)
            app.state.scheduler_metrics_store = None
            app.state.scheduler_metrics_collector = None
            app.state.trace_console_reader.close()
            close_all_pools()
            if revalidate_task is not None:
                await app.state.license_guard.stop_periodic_revalidation()
            registry = getattr(app.state, "agent_runtime_registry", None)
            if registry is not None:
                await registry.aclose()
            feishu_service = getattr(app.state, "feishu_knowledge_service", None)
            if feishu_service is not None:
                await feishu_service.aclose()
            await app.state.coach_courses.close()
            await app.state.learning_chapters.close()
            if knowledge_adapter is not None:
                knowledge_adapter.shutdown()
            shutdown_otel()

    app = FastAPI(title="ViKnow", version="0.1.0", lifespan=lifespan)
    setup_otel(app_config.observability.otel, app=app)
    app.state.config = app_config
    from viknow.core.trace_console.factory import build_trace_reader

    app.state.trace_console_reader = build_trace_reader(app_config)
    app.state.license_guard = license_guard
    app.state.license_locked = False
    app.state.profile_catalog = app_config.profile_catalog
    app.state.tool_configs = tool_configs
    app.state.runtime_tool_configs_ready = False
    app.state.knowledge_adapter = (
        build_hyperrag_adapter(app_config) if app_config.knowledge is not None else None
    )
    app.state.learning_chapters = ChapterService(
        lambda: app.state.config, lambda: app.state.knowledge_adapter
    )

    def coach_upload_manager():
        _ensure_knowledge_upload_manager(app)
        return app.state.knowledge_upload_manager

    app.state.coach_courses = CourseService(
        lambda: app.state.config,
        coach_upload_manager,
        lambda: app.state.knowledge_adapter,
        app.state.learning_chapters,
    )
    app.state.session_store = _create_session_store(app_config, app_config.profile_catalog)
    app.state.session_event_store = None
    app.state.learning_projection_service = None
    app.state.case_review_service = None
    app.state.langfuse_project_identity = langfuse_project_identity
    app.state.user_memory_service = None
    app.state.agent_runtime_registry = AgentRuntimeRegistry(
        catalog=app.state.profile_catalog,
        factory=lambda profile: _build_profile_runtime(app, profile),
    )
    app.state.request_timeout_seconds = app_config.api.request_timeout_seconds
    app.state.scheduler_metrics_store = None
    app.state.scheduler_metrics_collector = None
    app.state.scheduler_pool_overrides = {}
    app.state.multi_agent_enabled_override = None
    app.state.admin_config = app_config.admin
    app.state.feishu_knowledge_service = (
        build_feishu_knowledge_service(app_config.knowledge.feishu)
        if app_config.knowledge is not None
        else None
    )
    app.state.feishu_sync_store = None
    app.state.feishu_sync_coordinator = None
    app.state.feishu_sync_service_ref = None
    app.state.feishu_sync_scheduler = None
    app.state.vicoach_store = None
    app.state.vicoach_training_service = None

    web_dir = app_config.api.static_dir
    assets_dir = web_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    from viknow.api.case_review import create_case_review_router
    from viknow.api.trace_console import create_trace_console_router

    app.include_router(create_trace_console_router(web_dir))
    app.include_router(create_case_review_router())
    app.include_router(create_admin_router())
    app.include_router(create_coach_router(web_dir))
    app.include_router(create_chapter_router(lambda request: request.app.state.learning_chapters))
    app.include_router(
        create_feishu_knowledge_router(
            upload_manager_provider=request_knowledge_upload_manager,
            sync_coordinator_provider=request_feishu_sync_coordinator,
        )
    )
    app.include_router(
        create_vicoach_router(
            lambda: request_vicoach_training_service(app),
            chapter_reader_provider=lambda: request_vicoach_chapter_reader(app),
        )
    )

    @app.middleware("http")
    async def request_logging_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        started_at = perf_counter()
        status_code = 500
        with logging_context(request_id=request_id):
            try:
                if getattr(request.app.state, "license_locked", False) and (
                    request.url.path != "/api/v1/health/license"
                ):
                    response = JSONResponse(
                        status_code=503,
                        content={"detail": "license invalid; service locked"},
                    )
                else:
                    response = await call_next(request)
                status_code = response.status_code
            finally:
                duration_ms = int((perf_counter() - started_at) * 1000)
                access_log = logger.bind(source_name="api.access")
                log_line = "{} {} {} {}ms"
                log_args = (request.method, request.url.path, status_code, duration_ms)
                if _quiet_access_log(request.method, request.url.path, status_code):
                    access_log.debug(log_line, *log_args)
                else:
                    access_log.info(log_line, *log_args)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/api/v1/health/license")
    def license_health(request: Request) -> dict:
        guard: LicenseGuard = request.app.state.license_guard
        return guard.status().model_dump(mode="json")

    @app.get("/")
    def entry() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/login")
    def login() -> FileResponse:
        return FileResponse(web_dir / "login.html")

    @app.get("/chat")
    def chat() -> FileResponse:
        return FileResponse(web_dir / "chat.html")

    @app.get("/admin")
    def scheduler_admin() -> FileResponse:
        return FileResponse(web_dir / "admin.html")

    @app.get("/evaluator")
    def evaluator() -> FileResponse:
        return FileResponse(web_dir / "evaluator.html")

    @app.get("/index.html")
    def index_view() -> FileResponse:
        return FileResponse(web_dir / "index-graph.html")

    @app.post("/api/agent/submit", response_model=SubmitResult)
    async def submit(request: Request, payload: SubmitRequest) -> SubmitResult:
        with logging_context(
            user_id=payload.user_id,
            session_id=payload.session_id,
            workspace_id=payload.workspace_id,
        ):
            try:
                registry = request_runtime_registry(request)
                runtime = await registry.get(registry.catalog.default_profile_id)
                result = await runtime.submit(
                    AgentOp(
                        type=AgentOpType.USER_INPUT,
                        input=payload.question,
                        session_id=payload.session_id,
                        user_id=payload.user_id,
                        workspace_id=payload.workspace_id,
                        agent_model=payload.agent_model,
                    )
                )
                if result.run_id:
                    registry.bind_run(result.run_id, runtime)
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result

    @app.post("/api/v1/knowledge/indexes", response_model=CreateKnowledgeIndexResponse)
    async def create_knowledge_index(
        request: Request,
        payload: CreateKnowledgeIndexRequest,
    ) -> CreateKnowledgeIndexResponse:
        try:
            result = await request_knowledge_adapter(request).submit_index_async(
                library_id=payload.library_id,
                asset_id=payload.asset_id,
                asset_path=payload.asset_path,
                compile_mode=payload.compile_mode,
                params=payload.params.model_dump(exclude_none=True) if payload.params else None,
                request_id=request.state.request_id,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return CreateKnowledgeIndexResponse(**result.__dict__)

    @app.get(
        "/api/v1/online/libraries/{library_id}/assets/{asset_id}/index",
        response_model=KnowledgeAssetIndexResponse,
    )
    def get_chunk_list(
        request: Request,
        library_id: str,
        asset_id: str,
    ) -> KnowledgeAssetIndexResponse:
        library_id = _validate_library_id(library_id)
        asset_id = _validate_asset_id(asset_id)
        adapter = request_knowledge_adapter(request)
        try:
            status = adapter.get_asset_status(library_id=library_id, asset_id=asset_id)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if status is None:
            raise HTTPException(status_code=404, detail="asset not found")

        raw_status = _asset_status_value(status)
        if raw_status == "deleted":
            raise HTTPException(status_code=404, detail="asset not found")
        try:
            index_status = _online_index_status(raw_status)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if index_status != "completed":
            return KnowledgeAssetIndexResponse(
                library_id=library_id,
                asset_id=asset_id,
                index_status=index_status,
                items=[],
            )

        try:
            chunks = adapter.list_asset_segments(
                library_id=library_id,
                asset_id=asset_id,
            )
            items = IndexVisualizationService().project_chunks(
                library_id=library_id,
                asset_id=asset_id,
                chunks=chunks,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return KnowledgeAssetIndexResponse(
            library_id=library_id,
            asset_id=asset_id,
            index_status=index_status,
            items=[KnowledgeIndexItemResponse(**item.model_dump()) for item in items],
        )

    @app.post("/api/v1/knowledge/index-jobs", response_model=KnowledgeIndexJobResponse)
    async def create_knowledge_index_job(
        request: Request,
        payload: CreateKnowledgeIndexRequest,
    ) -> KnowledgeIndexJobResponse:
        asset_path = _require_index_asset_path(payload.asset_path)
        try:
            result = await request_knowledge_adapter(request).submit_index_async(
                library_id=payload.library_id,
                asset_id=payload.asset_id,
                asset_path=str(asset_path),
                compile_mode=payload.compile_mode,
                params=payload.params.model_dump(exclude_none=True) if payload.params else None,
                request_id=request.state.request_id,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return KnowledgeIndexJobResponse(
            job_id=result.job_id,
            library_id=result.library_id,
            asset_id=result.asset_id,
            status=_index_job_public_status(result.status),
        )

    @app.get("/api/v1/knowledge/index-jobs/{job_id}", response_model=KnowledgeIndexJobResponse)
    def get_knowledge_index_job(
        request: Request,
        job_id: str,
    ) -> KnowledgeIndexJobResponse:
        job_id = job_id.strip()
        if not job_id:
            raise HTTPException(status_code=422, detail="job_id is required")
        reader = request_job_progress_reader(request)
        job = reader.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="index job not found")
        job_record = _index_job_redis_record(reader, job.job_id)
        asset_path = job_record.get("asset_path")
        return _knowledge_index_job_response(
            request,
            job_id=job.job_id,
            library_id=job.workspace,
            asset_id=job.asset_id,
            status=job.status,
            error_message=job.error_message or None,
            asset_path=str(asset_path) if asset_path else None,
        )

    @app.post("/api/v1/knowledge/search")
    async def search_knowledge(
        request: Request,
        payload: KnowledgeSearchRequest,
    ) -> dict[str, Any]:
        handler = KnowledgeToolHandlers(
            knowledge_adapter=request_knowledge_adapter(request),
        )
        from viknow.core.tools.base import unwrap_tool_data

        raw = await handler.knowledge_search_async(payload.model_dump())
        return unwrap_tool_data(json.loads(raw))

    @app.delete("/api/v1/knowledge/indexes", response_model=DeleteKnowledgeIndexResponse)
    async def delete_knowledge_index(
        request: Request,
        payload: DeleteKnowledgeIndexRequest,
    ) -> DeleteKnowledgeIndexResponse:
        try:
            result = await request_knowledge_adapter(request).delete_index_async(
                library_id=payload.library_id,
                asset_id=payload.asset_id,
                request_id=request.state.request_id,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return DeleteKnowledgeIndexResponse(**result.__dict__)

    @app.post(
        "/api/v1/knowledge/precompile/export",
        response_model=ExportKnowledgePackageResponse,
    )
    async def export_knowledge_package(
        request: Request,
        payload: ExportKnowledgePackageRequest,
    ) -> ExportKnowledgePackageResponse:
        try:
            package = await request_knowledge_adapter(request).export_knowledge_package_async(
                workspaces=payload.workspaces,
                company_replacements=payload.company_replacements,
                alias_fullname_map=payload.alias_fullname_map,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ExportKnowledgePackageResponse(package=package)

    @app.post(
        "/api/v1/knowledge/precompile/import",
        response_model=ImportKnowledgePackageResponse,
    )
    async def import_knowledge_package(
        request: Request,
        payload: ImportKnowledgePackageRequest,
    ) -> ImportKnowledgePackageResponse:
        try:
            result = await request_knowledge_adapter(request).import_knowledge_package_async(
                package=payload.package,
                company_replacements=payload.company_replacements,
                alias_fullname_map=payload.alias_fullname_map,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return ImportKnowledgePackageResponse(**result)

    @app.post(
        "/api/v1/knowledge/migration/export",
        response_model=MigrationJobAcceptedResponse,
    )
    async def create_knowledge_migration_export(
        request: Request,
        payload: MigrationExportRequest,
    ) -> MigrationJobAcceptedResponse:
        service = request_knowledge_migration_service(request)
        job_id = await service.submit_export(payload.library_id)
        return MigrationJobAcceptedResponse(
            job_id=job_id,
            kind="export",
            library_id=payload.library_id,
            status="queued",
        )

    @app.post(
        "/api/v1/knowledge/migration/import",
        response_model=MigrationJobAcceptedResponse,
    )
    async def create_knowledge_migration_import(
        request: Request,
        file: Annotated[UploadFile, File()],
        library_id: Annotated[str | None, Form()] = None,
    ) -> MigrationJobAcceptedResponse:
        if not library_id or not library_id.strip():
            raise HTTPException(status_code=400, detail="library_id is required")
        target_library_id = _validate_library_id(library_id)
        service = request_knowledge_migration_service(request)
        incoming_dir = service.exports_root / "_incoming"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename or "package.tar.gz").name
        package_path = incoming_dir / f"{uuid4()}-{safe_name}"
        try:
            with package_path.open("wb") as sink:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to store package: {exc}") from exc
        job_id = await service.submit_import(target_library_id, package_path)
        return MigrationJobAcceptedResponse(
            job_id=job_id,
            kind="import",
            library_id=target_library_id,
            status="queued",
        )

    @app.post(
        "/api/v1/knowledge/migration/import-from-export",
        response_model=MigrationJobAcceptedResponse,
    )
    async def create_knowledge_migration_import_from_export(
        request: Request,
        payload: MigrationImportFromExportRequest,
    ) -> MigrationJobAcceptedResponse:
        source_library_id = _validate_library_id(payload.source_library_id)
        target_library_id = _validate_library_id(payload.target_library_id)
        service = request_knowledge_migration_service(request)
        try:
            package_path = service.resolve_export_package(source_library_id, payload.filename)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        job_id = await service.submit_import(target_library_id, package_path)
        return MigrationJobAcceptedResponse(
            job_id=job_id,
            kind="import",
            library_id=target_library_id,
            status="queued",
        )

    @app.get(
        "/api/v1/knowledge/migration/jobs/{job_id}",
        response_model=MigrationJobResponse,
    )
    async def get_knowledge_migration_job(
        request: Request,
        job_id: str,
    ) -> MigrationJobResponse:
        service = request_knowledge_migration_service(request)
        job = await service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="migration job not found")
        return _migration_job_response(job)

    @app.get(
        "/api/v1/knowledge/migration/jobs",
        response_model=list[MigrationJobResponse],
    )
    async def list_knowledge_migration_jobs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[MigrationJobResponse]:
        service = request_knowledge_migration_service(request)
        jobs = await service.list_jobs(limit=limit)
        return [_migration_job_response(job) for job in jobs]

    @app.get(
        "/api/v1/knowledge/exports",
        response_model=MigrationExportListResponse,
    )
    async def list_all_knowledge_exports(
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 10,
        library_id: Annotated[str | None, Query()] = None,
    ) -> MigrationExportListResponse:
        service = request_knowledge_migration_service(request)
        if library_id is not None and library_id.strip():
            filtered_library_id = _validate_library_id(library_id)
            exports = service.list_exports(filtered_library_id)
        else:
            filtered_library_id = None
            exports = service.list_all_exports()
        total = len(exports)
        start = (page - 1) * page_size
        page_items = exports[start : start + page_size]
        return MigrationExportListResponse(
            library_id=filtered_library_id,
            exports=[
                MigrationExportFile(
                    library_id=item.get("library_id") or filtered_library_id,
                    filename=item["filename"],
                    size_bytes=item["size_bytes"],
                    created_at=datetime.fromtimestamp(item["created_at"]),
                )
                for item in page_items
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get(
        "/api/v1/knowledge/exports/{library_id}",
        response_model=MigrationExportListResponse,
    )
    async def list_knowledge_exports(
        request: Request,
        library_id: str,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> MigrationExportListResponse:
        _validate_library_id(library_id)
        service = request_knowledge_migration_service(request)
        exports = service.list_exports(library_id)
        total = len(exports)
        start = (page - 1) * page_size
        page_items = exports[start : start + page_size]
        return MigrationExportListResponse(
            library_id=library_id,
            exports=[
                MigrationExportFile(
                    library_id=library_id,
                    filename=item["filename"],
                    size_bytes=item["size_bytes"],
                    created_at=datetime.fromtimestamp(item["created_at"]),
                )
                for item in page_items
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/knowledge/exports/{library_id}/{filename}")
    async def download_knowledge_export(
        request: Request,
        library_id: str,
        filename: str,
    ) -> FileResponse:
        _validate_library_id(library_id)
        service = request_knowledge_migration_service(request)
        safe_name = Path(filename).name
        file_path = service.exports_root / library_id / safe_name
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="export package not found")
        return FileResponse(
            file_path,
            media_type="application/gzip",
            filename=safe_name,
        )

    @app.post(
        "/api/v1/knowledge/uploads",
        response_model=KnowledgeUploadResponse,
    )
    async def create_knowledge_upload(
        request: Request,
        file: Annotated[UploadFile, File()],
        client_upload_id: Annotated[UUID | None, Form()] = None,
        library_id: Annotated[str | None, Form()] = None,
    ) -> KnowledgeUploadResponse:
        manager = request_knowledge_upload_manager(request)
        filename = (file.filename or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="filename is required")
        try:
            record = await manager.upload_stream(
                filename=filename,
                stream=file.file,
                size_bytes=file.size,
                content_type=file.content_type,
                upload_id=str(client_upload_id) if client_upload_id is not None else None,
                library_id=library_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("knowledge upload failed")
            raise HTTPException(status_code=503, detail="上传失败，请检查 MinIO 配置") from exc
        return _knowledge_upload_response(record)

    @app.get(
        "/api/v1/knowledge/uploads",
        response_model=KnowledgeUploadListResponse,
    )
    def list_knowledge_uploads(request: Request) -> KnowledgeUploadListResponse:
        manager = request_knowledge_upload_manager(request)
        return KnowledgeUploadListResponse(
            uploads=[_knowledge_upload_response(item) for item in manager.list_uploads()],
        )

    @app.get(
        "/api/v1/knowledge/uploads/{upload_id}",
        response_model=KnowledgeUploadResponse,
    )
    def get_knowledge_upload(
        request: Request,
        upload_id: str,
    ) -> KnowledgeUploadResponse:
        record = request_knowledge_upload_manager(request).get_upload(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail="upload not found")
        return _knowledge_upload_response(record)

    @app.delete(
        "/api/v1/knowledge/uploads/{upload_id}",
        response_model=DeleteKnowledgeUploadResponse,
    )
    async def delete_knowledge_upload(
        request: Request,
        upload_id: str,
    ) -> DeleteKnowledgeUploadResponse:
        deleted = await request_knowledge_upload_manager(request).delete_upload(upload_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="upload not found")
        return DeleteKnowledgeUploadResponse(upload_id=upload_id, deleted=True)

    @app.get(
        "/api/v1/knowledge/uploads/{upload_id}/video-summary",
        response_model=KnowledgeUploadVideoSummaryResponse,
    )
    async def get_knowledge_upload_video_summary(
        request: Request,
        upload_id: str,
    ) -> KnowledgeUploadVideoSummaryResponse:
        manager = request_knowledge_upload_manager(request)
        try:
            payload = await manager.get_video_global_summary(upload_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return KnowledgeUploadVideoSummaryResponse(**payload)

    @app.put(
        "/api/v1/knowledge/uploads/{upload_id}/video-summary",
        response_model=UpdateKnowledgeUploadVideoSummaryResponse,
    )
    async def put_knowledge_upload_video_summary(
        request: Request,
        upload_id: str,
        payload: UpdateKnowledgeUploadVideoSummaryRequest,
    ) -> UpdateKnowledgeUploadVideoSummaryResponse:
        manager = request_knowledge_upload_manager(request)
        try:
            updated, job_id, record = await manager.update_video_global_summary(
                upload_id=upload_id,
                global_summary=payload.global_summary,
                request_id=request.state.request_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return UpdateKnowledgeUploadVideoSummaryResponse(
            upload_id=record.id,
            library_id=record.library_id,
            asset_id=record.asset_id,
            updated=updated,
            job_id=job_id,
        )

    @app.post("/api/v1/users", response_model=UserResponse)
    async def create_user(request: Request) -> UserResponse:
        user = request_session_manager(request).create_user()
        return _user_response(user)

    @app.get("/api/v1/agent/models", response_model=AgentModelsResponse)
    async def agent_models(request: Request) -> AgentModelsResponse:
        config = request_app_config(request)
        return AgentModelsResponse(
            default_model=config.agent.default_model_id() or "",
            models=[
                {"id": option.selection_id(), "label": option.label or option.selection_id()}
                for option in config.agent.model_options
            ],
        )

    @app.get("/api/v1/agent/profiles", response_model=AgentProfilesResponse)
    async def agent_profiles(request: Request) -> AgentProfilesResponse:
        catalog = request_profile_catalog(request)
        return AgentProfilesResponse(
            default_profile=catalog.default_profile_id,
            profiles=[
                AgentProfileOptionResponse(
                    id=profile.id,
                    label=profile.label,
                    description=profile.description,
                )
                for profile in catalog.all()
            ],
        )

    @app.get("/api/v1/users/{user_id}", response_model=UserResponse)
    async def get_user(request: Request, user_id: UUID) -> UserResponse:
        try:
            return _user_response(request_session_manager(request).get_user(str(user_id)))
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc

    @app.get("/api/v1/users/{user_id}/preferences", response_model=UserPreferences)
    async def get_user_preferences(request: Request, user_id: UUID) -> UserPreferences:
        try:
            user = request_session_manager(request).get_user(str(user_id))
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        preferences = await request_user_memory_service(request).get_preferences(str(user_id))
        return preferences.model_copy(update={"memory_enabled": user.memory_enabled})

    @app.put("/api/v1/users/{user_id}/preferences", response_model=UserPreferences)
    async def put_user_preferences(
        request: Request,
        user_id: UUID,
        payload: UserPreferences,
    ) -> UserPreferences:
        manager = request_session_manager(request)
        try:
            user = manager.update_user_memory_enabled(
                user_id=str(user_id),
                memory_enabled=payload.memory_enabled,
            )
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        saved = await request_user_memory_service(request).save_preferences(
            str(user_id),
            UserPreferences(
                persona=payload.persona,
                occupation=payload.occupation,
                answer_style=payload.answer_style,
                custom_notes=payload.custom_notes,
            ),
        )
        return saved.model_copy(update={"memory_enabled": user.memory_enabled})

    @app.get("/api/v1/users/{user_id}/sessions", response_model=list[SessionResponse])
    async def list_sessions(request: Request, user_id: UUID) -> list[SessionResponse]:
        try:
            sessions = request_session_manager(request).list_sessions(str(user_id))
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        return [_session_response(session) for session in sessions]

    @app.get("/api/v1/users/{user_id}/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(request: Request, user_id: UUID, session_id: UUID) -> SessionResponse:
        try:
            session = request_session_manager(request).get_session(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return _session_response(session)

    @app.delete("/api/v1/users/{user_id}/sessions/{session_id}", status_code=204)
    async def delete_session(request: Request, user_id: UUID, session_id: UUID) -> None:
        try:
            request_session_manager(request).delete_session(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except SessionBusy as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "session_busy", "message": "session has a running run"},
            ) from exc

    @app.post(
        "/api/v1/users/{user_id}/sessions/{session_id}/files",
        response_model=SessionFileResponse,
    )
    async def upload_session_file(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        file: Annotated[UploadFile, File()],
    ) -> SessionFileResponse:
        request_filesystem_manager(request)
        content = await file.read()
        try:
            record = request_session_manager(request).upload_session_file(
                user_id=str(user_id),
                session_id=str(session_id),
                file_name=file.filename or "attachment.bin",
                content=content,
                mime_type=file.content_type,
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except SessionFileTooLarge as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "file_too_large",
                    "message": str(exc),
                    "max_file_mb": exc.max_file_mb,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _session_file_response(record)

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/files",
        response_model=SessionFileListResponse,
    )
    async def list_session_files(
        request: Request,
        user_id: UUID,
        session_id: UUID,
    ) -> SessionFileListResponse:
        request_filesystem_manager(request)
        try:
            files = request_session_manager(request).list_session_files(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        workspace = resolve_deepagents_config(request_app_config(request)).workspace
        return SessionFileListResponse(
            files=[_session_file_response(item) for item in files],
            upload_limits={
                "max_file_mb": workspace.upload.max_file_mb,
                "allowed_extensions": list(workspace.upload.allowed_extensions),
            },
            parse_limits={
                "enabled": workspace.parse.enabled,
                "allowed_extensions": list(workspace.parse.allowed_extensions),
            },
        )

    @app.get("/api/v1/users/{user_id}/sessions/{session_id}/files/{file_id}/content")
    async def download_session_file(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        file_id: str,
    ) -> StreamingResponse:
        request_filesystem_manager(request)
        try:
            record, stream = request_session_manager(request).open_session_file_stream(
                user_id=str(user_id),
                session_id=str(session_id),
                file_id=file_id,
            )
        except (UserNotFound, SessionNotFound, SessionFileNotFound) as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        return StreamingResponse(
            stream,
            media_type=record.mime_type or "application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{record.file_name}"',
            },
        )

    @app.delete(
        "/api/v1/users/{user_id}/sessions/{session_id}/files/{file_id}",
        status_code=204,
    )
    async def delete_session_file(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        file_id: str,
    ) -> None:
        request_filesystem_manager(request)
        try:
            request_session_manager(request).delete_session_file(
                user_id=str(user_id),
                session_id=str(session_id),
                file_id=file_id,
            )
        except (UserNotFound, SessionNotFound, SessionFileNotFound) as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
        except SessionBusy as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "session_busy", "message": "session has a running run"},
            ) from exc

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/events",
        response_model=list[AgentEvent],
    )
    async def session_events(
        request: Request,
        user_id: UUID,
        session_id: UUID,
    ) -> list[AgentEvent]:
        try:
            return request_session_manager(request).list_session_events(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/timeline",
        response_model=SessionTimeline,
    )
    async def session_timeline(
        request: Request,
        user_id: UUID,
        session_id: UUID,
    ) -> SessionTimeline:
        try:
            return request_session_manager(request).session_timeline(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/tool_calls/{call_id}",
        response_model=ToolCallDetail,
    )
    async def session_tool_call_detail(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        call_id: str,
    ) -> ToolCallDetail:
        try:
            return request_session_manager(request).tool_call_detail(
                user_id=str(user_id),
                session_id=str(session_id),
                call_id=call_id,
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except ToolCallNotFound as exc:
            raise HTTPException(status_code=404, detail="tool call not found") from exc

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/runs/{run_id}/reasoning",
        response_model=ReasoningDetail,
    )
    async def session_run_reasoning(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        run_id: str,
    ) -> ReasoningDetail:
        try:
            return request_session_manager(request).reasoning_detail(
                user_id=str(user_id),
                session_id=str(session_id),
                run_id=run_id,
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/runs/{run_id}/answer_review",
        response_model=AnswerReviewDetail,
    )
    async def session_run_answer_review(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        run_id: str,
    ) -> AnswerReviewDetail:
        try:
            return request_session_manager(request).answer_review_detail(
                user_id=str(user_id),
                session_id=str(session_id),
                run_id=run_id,
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post(
        "/api/v1/users/{user_id}/sessions/{session_id}/runs/{run_id}/learning-feedback",
        response_model=LearningFeedbackResponse,
    )
    async def submit_learning_feedback(
        request: Request,
        user_id: UUID,
        session_id: UUID,
        run_id: str,
        payload: SubmitLearningFeedbackRequest,
    ) -> LearningFeedbackResponse:
        try:
            request_session_manager(request).get_session(
                user_id=str(user_id),
                session_id=str(session_id),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        run = request_session_store(request).get_run(run_id)
        if run is None or run.session_id != str(session_id) or run.user_id != str(user_id):
            raise HTTPException(status_code=404, detail="run not found")
        try:
            feedback = request_learning_projection_service(request).submit_feedback(
                run_id=run_id,
                disposition=payload.disposition,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return LearningFeedbackResponse(run_id=run_id, **feedback.model_dump())

    @app.get(
        "/api/v1/users/{user_id}/sessions/{session_id}/debug",
        response_model=SessionDebug,
    )
    async def session_debug(
        request: Request,
        user_id: UUID,
        session_id: UUID,
    ) -> SessionDebug:
        try:
            service = request_session_manager(request)
            session = service.get_session(user_id=str(user_id), session_id=str(session_id))
            return service.session_debug(
                user_id=str(user_id),
                session_id=str(session_id),
                workspace_dir=_session_workspace_dir(request_app_config(request), session),
            )
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    @app.post("/api/v1/agent/runs", response_model=CreateRunResponse)
    async def create_run(request: Request) -> CreateRunResponse:
        try:
            payload, upload_files = await _parse_create_run_request(request)
            if upload_files or payload.attachment_file_ids:
                request_filesystem_manager(request)
            result = await request_session_manager(request).create_run(
                user_id=str(payload.user_id),
                session_id=str(payload.session_id) if payload.session_id is not None else None,
                message=payload.message,
                profile_id=payload.profile_id,
                agent_model=payload.agent_model,
                use_precompiled_knowledge=payload.use_precompiled_knowledge,
                attachment_file_ids=payload.attachment_file_ids,
                library_ids=payload.library_ids,
                upload_files=upload_files,
            )
        except UnknownProfile as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_profile", "message": "unknown agent profile"},
            ) from exc
        except (UserNotFound, SessionNotFound) as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except SessionProfileConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_profile_conflict",
                    "message": "session profile cannot be changed",
                },
            ) from exc
        except SessionProfileUnavailable as exc:
            raise _session_profile_unavailable_http_error(exc) from exc
        except SessionBusy as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "session_busy", "message": "session has a running run"},
            ) from exc
        except InvalidAttachmentFiles as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_attachment_file_ids",
                    "message": "attachment_file_ids must belong to the session",
                    "file_ids": exc.file_ids,
                },
            ) from exc
        except SessionFileTooLarge as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "file_too_large",
                    "message": str(exc),
                    "max_file_mb": exc.max_file_mb,
                },
            ) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_context=False),
            ) from exc
        except RunSubmissionFailed as exc:
            raise _run_submission_failed_http_error(exc) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return CreateRunResponse(**result.model_dump())

    @app.post("/api/v1/eval/runs", response_model=EvalRunResponse)
    async def eval_run(request: Request, payload: EvalRunRequest) -> EvalRunResponse:
        service = request_eval_service(request)
        cached_eval_user_id = getattr(request.app.state, "eval_user_id", None)
        request_user_id = str(payload.user_id) if payload.user_id is not None else None
        try:
            trace = await service.eval_run(
                query=payload.query,
                profile_id=payload.profile_id,
                evidence_collection_task=payload.evidence_collection_task,
                timeout_seconds=payload.timeout_seconds,
                agent_model=payload.agent_model,
                requested_trace_id=payload.trace_id,
                trace_name=payload.trace_name,
                request_user_id=request_user_id,
                cached_eval_user_id=cached_eval_user_id,
                skill_flow_gold_trace=payload.gold_trace,
                selected_knowledge_assets=payload.selected_knowledge_assets,
                library_ids=payload.library_ids,
            )
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        except EvalRunTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RunSubmissionFailed as exc:
            raise _run_submission_failed_http_error(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if request_user_id is None and trace.user_id:
            request.app.state.eval_user_id = trace.user_id
        return trace

    @app.post(
        "/api/v1/eval/inspect-tool/run-case",
        response_model=InspectToolEvalRunCaseResponse,
    )
    def inspect_tool_eval_run_case(
        request: Request,
        payload: InspectToolEvalRunCaseRequest,
    ) -> dict:
        service = request_inspect_tool_eval_service(request)
        if payload.judge_timeout_seconds is not None:
            service.judge_timeout_seconds = payload.judge_timeout_seconds
        try:
            return service.run_case(
                InspectToolEvalCase(
                    case_id=payload.case_id,
                    tool=payload.tool,
                    arguments=payload.arguments,
                    metadata=payload.metadata,
                )
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post(
        "/api/v1/eval/inspect-tool/rejudge-case",
        response_model=InspectToolEvalRejudgeCaseResponse,
    )
    def inspect_tool_eval_rejudge_case(
        request: Request,
        payload: InspectToolEvalRejudgeCaseRequest,
    ) -> dict:
        service = request_inspect_tool_eval_service(request)
        if payload.judge_timeout_seconds is not None:
            service.judge_timeout_seconds = payload.judge_timeout_seconds
        case = InspectToolEvalCase(
            case_id=payload.case_id,
            tool=payload.tool,
            arguments=payload.arguments,
            metadata=payload.metadata,
        )
        attempts: list[dict[str, Any]] = []
        for attempt_index in range(1, payload.repeat_count + 1):
            try:
                judged_run = service.judge_case(case, dict(payload.frozen_run))
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            attempts.append(
                {
                    "attempt": attempt_index,
                    "judge_result": judged_run.get("judge_result"),
                    "telemetry": judged_run.get("telemetry"),
                }
            )
        return {
            "case_id": payload.case_id,
            "tool": payload.tool,
            "repeat_count": payload.repeat_count,
            "attempts": attempts,
        }

    @app.post(
        "/api/v1/eval/inspect-tool/debug/advanced-video",
        response_model=InspectToolEvalRunCaseResponse,
    )
    def video_inspect_2_0_debug(
        request: Request,
        payload: InspectToolEvalRunCaseRequest,
    ) -> dict:
        if payload.tool != "video_inspect_2_0":
            raise HTTPException(
                status_code=400,
                detail="advanced video debug only supports video_inspect_2_0",
            )
        if not getattr(request.app.state, "runtime_tool_configs_ready", False):
            request.app.state.tool_configs = materialize_runtime_tool_configs(
                request.app.state.config,
                request.app.state.tool_configs,
            )
            request.app.state.runtime_tool_configs_ready = True
        service = InspectToolEvalService(
            judge=None,
            repo_root=_workspace_root(),
            tool_configs=request.app.state.tool_configs,
            knowledge_adapter=getattr(request.app.state, "knowledge_adapter", None),
            filesystem_manager=getattr(request.app.state, "filesystem_manager", None),
        )
        try:
            return service.run_case(
                InspectToolEvalCase(
                    case_id=payload.case_id,
                    tool=payload.tool,
                    arguments=payload.arguments,
                    metadata=payload.metadata,
                )
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/v1/eval/locate-anything/requests")
    def locate_anything_eval_requests(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        client = request_locate_anything_eval_client(request)
        return {"requests": client.recent_requests(limit=limit)}

    @app.post("/api/v1/eval/locate-anything/locate")
    def locate_anything_eval_locate(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        image_base64 = str(payload.get("image_base64") or "").strip()
        query = str(payload.get("query") or "").strip()
        if not image_base64 or not query:
            raise HTTPException(status_code=400, detail="image_base64 and query are required")
        client = request_locate_anything_eval_client(request)
        result = client.locate_base64(
            image_base64,
            query=query,
            filename=str(payload.get("filename") or "upload.png"),
            mime_type=str(payload.get("mime_type") or "image/png"),
            mode=str(payload.get("mode") or "") or None,
            max_results=_optional_eval_int(payload.get("max_results")),
            generation_mode=str(payload.get("generation_mode") or "") or None,
        )
        return result.model_dump(mode="json")

    @app.post("/api/v1/agent/runs/{run_id}/interrupt", response_model=SubmitResult)
    async def interrupt_run(request: Request, run_id: str) -> SubmitResult:
        try:
            run = request_session_store(request).get_run(run_id)
            if run is not None and run.status != RunStatus.RUNNING:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "run_not_running", "message": "run is not running"},
                )
            runtime = await request_runtime_for_run(request, run_id)
            return await runtime.submit(AgentOp(type=AgentOpType.INTERRUPT, run_id=run_id))
        except SessionProfileUnavailable as exc:
            raise _session_profile_unavailable_http_error(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/agent/events/{run_id}")
    async def events(
        request: Request,
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        return await _events_response(request, run_id, after_sequence)

    @app.get("/api/v1/agent/runs/{run_id}/events")
    async def v1_events(
        request: Request,
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        return await _events_response(request, run_id, after_sequence)

    @app.get("/api/agent/status/{run_id}", response_model=AgentStatus)
    async def status(request: Request, run_id: str) -> AgentStatus:
        try:
            return await request_status_for_run(request, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/v1/agent/runs/{run_id}", response_model=AgentStatus)
    async def v1_status(request: Request, run_id: str) -> AgentStatus:
        try:
            return await request_status_for_run(request, run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/media/libraries/{library_id}/assets/{asset_id}/video")
    async def library_asset_video(request: Request, library_id: str, asset_id: str) -> FileResponse:
        adapter = request_knowledge_adapter(request)
        file_path_str = adapter.resolve_asset_file_path(library_id=library_id, asset_id=asset_id)
        if not file_path_str:
            raise HTTPException(status_code=404, detail="asset video not found")
        path = Path(file_path_str)
        if not path.is_absolute():
            path = _workspace_root() / path
        path = path.resolve()
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="asset video file not found")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/libraries/{library_id}/assets/{asset_id}/document")
    async def library_asset_document(
        request: Request, library_id: str, asset_id: str
    ) -> FileResponse:
        adapter = request_knowledge_adapter(request)
        path = adapter.resolve_document_render_path(library_id=library_id, asset_id=asset_id)
        if path is None or not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="document render source not found")
        media_type = "application/pdf" if path.suffix.lower() == ".pdf" else None
        return FileResponse(path, media_type=media_type)

    @app.get("/media/libraries/{library_id}/assets/{asset_id}/document/pages/{page_number}.png")
    async def library_asset_document_page(
        request: Request,
        library_id: str,
        asset_id: str,
        page_number: int,
        scale: float = Query(default=2.0, ge=0.5, le=4.0),
    ) -> Response:
        adapter = request_knowledge_adapter(request)
        path = adapter.resolve_document_render_path(library_id=library_id, asset_id=asset_id)
        if path is None or not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="document render source not found")
        try:
            image = _render_document_page_image(path, page_number=page_number, scale=scale)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="document page not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")

    @app.get("/media/libraries/{library_id}/assets/{asset_id}/images/{image_name:path}")
    async def library_asset_image(
        request: Request, library_id: str, asset_id: str, image_name: str
    ) -> FileResponse:
        adapter = request_knowledge_adapter(request)
        path = adapter.resolve_asset_image_path(
            library_id=library_id,
            asset_id=asset_id,
            image_name=image_name,
        )
        if path is None or not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="asset image not found")
        return FileResponse(path)

    @app.get("/media/libraries/{library_id}/assets/{asset_id}/clips/{begin_ts}/{end_ts}")
    async def library_asset_clip(
        request: Request, library_id: str, asset_id: str, begin_ts: float, end_ts: float
    ) -> FileResponse:
        adapter = request_knowledge_adapter(request)
        clip_path = adapter.resolve_clip_path(
            library_id=library_id, asset_id=asset_id, begin_ts=begin_ts, end_ts=end_ts
        )
        if clip_path is None or not clip_path.exists() or not clip_path.is_file():
            raise HTTPException(status_code=404, detail="clip not found")
        return FileResponse(clip_path, media_type="video/mp4")

    return app


async def _events_response(
    request: Request,
    run_id: str,
    after_sequence: int = 0,
) -> StreamingResponse:
    run = request_session_store(request).get_run(run_id)
    runtime: AgentRuntime | None = None
    if run is not None and run.status != RunStatus.RUNNING:
        replay = request_session_store(request).events_after(run_id, after_sequence)
        should_tail = False
    else:
        try:
            runtime = await request_runtime_for_run(request, run_id)
            if run is None:
                status = await runtime.get_status(run_id)
                should_tail = status.status == AgentStatusType.RUNNING
            else:
                should_tail = True
            replay = await runtime.events_after(run_id, after_sequence)
        except SessionProfileUnavailable as exc:
            raise _session_profile_unavailable_http_error(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    async def stream():
        cursor = after_sequence
        for event in replay:
            cursor = max(cursor, event.sequence)
            yield _sse_event(event)
            if _is_terminal_event(event):
                return
        if not should_tail or runtime is None:
            return
        while True:
            event = await runtime.next_event(run_id)
            if event.sequence <= cursor:
                continue
            cursor = event.sequence
            yield _sse_event(event)
            if _is_terminal_event(event):
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


def _sse_event(event) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _render_document_page_image(path: Path, *, page_number: int, scale: float) -> Image.Image:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        if page_number != 0:
            raise IndexError(page_number)
        return Image.open(path).convert("RGB")
    if suffix != ".pdf":
        raise ValueError(f"unsupported document render source: {path.name}")

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    if page_number < 0 or page_number >= len(document):
        raise IndexError(page_number)
    page = document[page_number]
    bitmap = page.render(scale=scale)
    return bitmap.to_pil().convert("RGB")


def _is_terminal_event(event) -> bool:
    return event.type in {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_INTERRUPTED,
    }


def request_user_memory_service(request: Request) -> UserMemoryService:
    service = getattr(request.app.state, "user_memory_service", None)
    if service is not None:
        return service
    service = build_user_memory_service(request_app_config(request))
    request.app.state.user_memory_service = service
    return service


def request_filesystem_manager(request: Request) -> FilesystemManager:
    _ensure_filesystem_manager(request.app)
    return request.app.state.filesystem_manager


def request_session_manager(request: Request) -> SessionManager:
    return SessionManager(
        store=request_session_store(request),
        catalog=request_profile_catalog(request),
        runtime_registry=request_runtime_registry(request),
        filesystem_manager=getattr(request.app.state, "filesystem_manager", None),
    )


def _session_file_response(record) -> SessionFileResponse:
    return SessionFileResponse(
        file_id=record.id,
        file_name=record.file_name,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        kind=record.kind.value,
        status=record.status.value,
        virtual_path=record.virtual_path,
        uploaded_at=record.uploaded_at,
    )


def request_session_store(request: Request) -> SessionStore:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise RuntimeError("session store is not initialized")
    return store


def request_learning_projection_service(request: Request) -> LearningProjectionService:
    service = getattr(request.app.state, "learning_projection_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="learning projection is disabled")
    return service


def request_eval_service(request: Request) -> EvalService:
    return EvalService(
        session_manager=request_session_manager(request),
        store=request_session_store(request),
        knowledge_adapter=getattr(request.app.state, "knowledge_adapter", None),
    )


def request_inspect_tool_eval_service(request: Request) -> InspectToolEvalService:
    if not getattr(request.app.state, "runtime_tool_configs_ready", False):
        request.app.state.tool_configs = materialize_runtime_tool_configs(
            request.app.state.config,
            request.app.state.tool_configs,
        )
        request.app.state.runtime_tool_configs_ready = True
    repo_root = _workspace_root()
    return InspectToolEvalService(
        judge=build_inspect_tool_judge(
            request.app.state.config.eval.inspect_tool.judge_model,
            repo_root=repo_root,
        ),
        judge_timeout_seconds=(
            request.app.state.config.eval.inspect_tool.judge_model.timeout_seconds
        ),
        repo_root=repo_root,
        tool_configs=request.app.state.tool_configs,
        knowledge_adapter=getattr(request.app.state, "knowledge_adapter", None),
        filesystem_manager=getattr(request.app.state, "filesystem_manager", None),
    )


def request_locate_anything_eval_client(request: Request) -> ActioLocateAnythingClient:
    if not getattr(request.app.state, "runtime_tool_configs_ready", False):
        request.app.state.tool_configs = materialize_runtime_tool_configs(
            request.app.state.config,
            request.app.state.tool_configs,
        )
        request.app.state.runtime_tool_configs_ready = True
    tool_configs = getattr(request.app.state, "tool_configs", None)
    multimodal = getattr(tool_configs, "multimodal", None)
    locate_config = getattr(multimodal, "locate_anything", None)
    if locate_config is None:
        raise HTTPException(status_code=503, detail="locate_anything tool config is unavailable")
    base_url = locate_config.resolve_base_url()
    if not base_url:
        raise HTTPException(status_code=503, detail="locate_anything base_url is unavailable")
    try:
        api_key = locate_config.require_api_key()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ActioLocateAnythingClient(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=locate_config.timeout_seconds,
    )


def _optional_eval_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="max_results must be an integer",
        ) from exc


def request_profile_catalog(request: Request) -> AgentProfileCatalog:
    catalog = getattr(request.app.state, "profile_catalog", None)
    if not isinstance(catalog, AgentProfileCatalog):
        raise RuntimeError("profile catalog is not initialized")
    return catalog


def request_runtime_registry(request: Request) -> AgentRuntimeRegistry:
    registry = getattr(request.app.state, "agent_runtime_registry", None)
    if (
        registry is None
        or not hasattr(registry, "catalog")
        or not callable(getattr(registry, "get", None))
    ):
        raise RuntimeError("agent runtime registry is not initialized")
    return registry


async def request_runtime_for_run(request: Request, run_id: str) -> AgentRuntime:
    if request_session_store(request).get_run(run_id) is not None:
        return await request_session_manager(request).runtime_for_run(run_id)
    registry = request_runtime_registry(request)
    return await registry.get(registry.catalog.default_profile_id)


async def request_status_for_run(request: Request, run_id: str) -> AgentStatus:
    run = request_session_store(request).get_run(run_id)
    if run is not None:
        return _managed_agent_status(run)
    runtime = await request_runtime_for_run(request, run_id)
    return await runtime.get_status(run_id)


def request_app_config(request: Request) -> AppConfig:
    config = getattr(request.app.state, "config", None)
    if not isinstance(config, AppConfig):
        raise RuntimeError("app config is not initialized")
    return config


def request_knowledge_adapter(request: Request) -> HyperRAGAdapter:
    adapter = getattr(request.app.state, "knowledge_adapter", None)
    if isinstance(adapter, HyperRAGAdapter):
        return adapter
    adapter = build_hyperrag_adapter(request_app_config(request))
    request.app.state.knowledge_adapter = adapter
    return adapter


def request_job_progress_reader(request: Request) -> HyperRAGJobProgressReader:
    reader = getattr(request.app.state, "job_progress_reader", None)
    if isinstance(reader, HyperRAGJobProgressReader):
        return reader
    reader = HyperRAGJobProgressReader(request_app_config(request))
    request.app.state.job_progress_reader = reader
    return reader


def _require_index_asset_path(asset_path: str) -> Path:
    path = Path(asset_path)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="asset_path must be an absolute path")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="asset_path does not exist or is not a file")
    return path


def _index_job_public_status(status: str) -> str:
    mapping = {
        "queued": "pending",
        "pending": "pending",
        "running": "running",
        "completed": "done",
        "done": "done",
        "failed": "failed",
        "cancelled": "failed",
    }
    return mapping.get(str(status or "").strip().lower(), "pending")


def _index_job_redis_record(reader: HyperRAGJobProgressReader, job_id: str) -> dict[str, Any]:
    from viknow.core.knowledge.job_progress import JOB_KEY_TEMPLATE

    raw = reader._client().get(JOB_KEY_TEMPLATE.format(job_id=job_id))
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _library_id_from_asset_path(asset_path: str | None) -> str | None:
    if not asset_path:
        return None
    parts = Path(asset_path).parts
    for marker in ("uploads", "libraries"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return parts[index + 1]
    return None


def _library_id_candidates(*, workspace: str, asset_path: str | None) -> list[str]:
    candidates: list[str] = []
    normalized_workspace = str(workspace or "").strip()
    if normalized_workspace:
        candidates.append(normalized_workspace)
    parsed_library_id = _library_id_from_asset_path(asset_path)
    if parsed_library_id and parsed_library_id not in candidates:
        candidates.append(parsed_library_id)
    return candidates


def _count_document_pages(file_path: str | None) -> int | None:
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            return len(PdfReader(str(path)).pages)
        if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}:
            return 1
    except Exception:
        return None
    return None


def _metadata_from_asset_status(asset_status: Any) -> dict[str, Any] | None:
    raw = getattr(asset_status, "metadata", None)
    if isinstance(raw, dict) and raw:
        return dict(raw)

    parse_mode = str(getattr(asset_status, "parse_mode", None) or "").strip().lower()
    file_path = getattr(asset_status, "file_path", None)
    metadata: dict[str, Any] = {}

    if parse_mode in {"video", "sop_video"}:
        metadata["doc_type"] = "sop_video" if parse_mode == "sop_video" else "video"
    elif parse_mode in {"document", "image"}:
        metadata["doc_type"] = "document"
        page_count = _count_document_pages(str(file_path) if file_path else None)
        if page_count is not None:
            metadata["page_count"] = page_count
    elif parse_mode:
        metadata["doc_type"] = parse_mode
    elif file_path:
        suffix = Path(str(file_path)).suffix.lower()
        if suffix == ".pdf" or suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp"}:
            metadata["doc_type"] = "document"
            page_count = _count_document_pages(str(file_path))
            if page_count is not None:
                metadata["page_count"] = page_count

    return metadata or None


def _get_asset_status(
    adapter: Any, *, library_id: str, asset_id: str
) -> Any | None:
    try:
        return adapter.get_asset_status(library_id=library_id, asset_id=asset_id)
    except (RuntimeError, ValueError):
        return None


def _resolve_index_job_metadata(
    adapter: Any, *, library_id: str, asset_id: str
) -> dict[str, Any] | None:
    asset_status = _get_asset_status(adapter, library_id=library_id, asset_id=asset_id)
    if asset_status is None:
        return None
    return _metadata_from_asset_status(asset_status)


def _knowledge_index_job_response(
    request: Request,
    *,
    job_id: str,
    library_id: str,
    asset_id: str,
    status: str,
    error_message: str | None,
    asset_path: str | None = None,
) -> KnowledgeIndexJobResponse:
    public_status = _index_job_public_status(status)
    resolved_library_id = library_id
    metadata = None
    if public_status == "done":
        adapter = request_knowledge_adapter(request)
        for candidate_library_id in _library_id_candidates(
            workspace=library_id,
            asset_path=asset_path,
        ):
            metadata = _resolve_index_job_metadata(
                adapter,
                library_id=candidate_library_id,
                asset_id=asset_id,
            )
            if metadata:
                resolved_library_id = candidate_library_id
                break
    return KnowledgeIndexJobResponse(
        job_id=job_id,
        library_id=resolved_library_id,
        asset_id=asset_id,
        status=public_status,
        error_message=error_message,
        metadata=metadata,
    )


def request_knowledge_upload_manager(request: Request) -> KnowledgeUploadManager:
    try:
        _ensure_knowledge_upload_manager(request.app)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    manager = getattr(request.app.state, "knowledge_upload_manager", None)
    if not isinstance(manager, KnowledgeUploadManager):
        raise HTTPException(status_code=503, detail="knowledge upload manager is not initialized")
    return manager


def request_vicoach_chapter_reader(app: FastAPI) -> PublishedChapterReader:
    config = app.state.config
    chapter_config = config.capabilities.vicoach.chapters
    jobs = (
        ChapterJobs(config.app.state_dir / "vicoach" / "chapters", chapter_config, None)
        if chapter_config is not None
        else None
    )
    return PublishedChapterReader(resolve_files_root(config), jobs)


def request_vicoach_training_service(app: FastAPI) -> VicoachTrainingService:
    service = getattr(app.state, "vicoach_training_service", None)
    if (
        isinstance(service, VicoachTrainingService)
        and getattr(app.state, "vicoach_service_config", None) is app.state.config
    ):
        return service
    config = app.state.config
    model_config = config.vicoach.model
    if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise RuntimeError(f"unsupported vicoach model provider: {model_config.provider}")
    base_url = model_config.resolve_base_url()
    if not base_url:
        raise RuntimeError("vicoach model base_url or base_url_env is required")
    from langchain_openai import ChatOpenAI

    question_llm = ChatOpenAI(
        model=model_config.require_model(),
        base_url=base_url,
        api_key=model_config.require_api_key(),
        temperature=config.vicoach.question_generation_temperature,
        timeout=model_config.timeout_seconds,
        extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
    )
    judge_llm = ChatOpenAI(
        model=model_config.require_model(),
        base_url=base_url,
        api_key=model_config.require_api_key(),
        temperature=model_config.temperature,
        timeout=model_config.timeout_seconds,
        extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
    )
    chapter_reader = request_vicoach_chapter_reader(app)
    store = getattr(app.state, "vicoach_store", None)
    if store is None:
        store = build_vicoach_store(config)
        app.state.vicoach_store = store
    service = VicoachTrainingService(
        service=VicoachService(
            chapter_reader=chapter_reader,
            question_generator=StructuredLLMQuestionGenerator(question_llm),
            free_text_judge=StructuredLLMFreeTextAnswerJudge(judge_llm),
            question_generation_concurrency=config.vicoach.question_generation_concurrency,
        ),
        chapter_reader=chapter_reader,
        store=store,
    )
    app.state.vicoach_training_service = service
    app.state.vicoach_service_config = config
    return service


def _ensure_knowledge_upload_manager(app: FastAPI) -> None:
    if getattr(app.state, "knowledge_upload_manager", None) is not None:
        return
    config = app.state.config
    if config.knowledge is None:
        raise RuntimeError("knowledge backend is not configured")
    upload_store = getattr(app.state, "knowledge_upload_store", None)
    if upload_store is None:
        upload_store = build_knowledge_upload_store(config)
        app.state.knowledge_upload_store = upload_store
    manager = KnowledgeUploadManager(
        store=upload_store,
        files_root=resolve_files_root(config),
        knowledge_config=config.knowledge,
        knowledge_adapter=app.state.knowledge_adapter or build_hyperrag_adapter(config),
    )
    app.state.knowledge_upload_manager = manager
    adapter = app.state.knowledge_adapter
    if isinstance(adapter, HyperRAGAdapter):
        adapter.set_evidence_metadata_resolver(manager.evidence_metadata)


def request_feishu_sync_coordinator(request: Request) -> FeishuSyncCoordinator:
    try:
        return _ensure_feishu_sync_coordinator(request.app)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _ensure_feishu_sync_coordinator(app: FastAPI) -> FeishuSyncCoordinator:
    service = getattr(app.state, "feishu_knowledge_service", None)
    if service is None:
        raise RuntimeError("飞书知识库接入未初始化")
    _ensure_knowledge_upload_manager(app)
    manager = app.state.knowledge_upload_manager
    existing = getattr(app.state, "feishu_sync_coordinator", None)
    if (
        isinstance(existing, FeishuSyncCoordinator)
        and getattr(app.state, "feishu_sync_service_ref", None) is service
        and existing.upload_manager is manager
    ):
        return existing
    sync_store = getattr(app.state, "feishu_sync_store", None)
    if sync_store is None:
        sync_store = build_feishu_sync_store(app.state.config)
        app.state.feishu_sync_store = sync_store
    coordinator = FeishuSyncCoordinator(
        service=service,
        upload_manager=manager,
        store=sync_store,
        default_interval_seconds=app.state.config.knowledge.feishu.default_sync_interval_seconds,
    )
    app.state.feishu_sync_coordinator = coordinator
    app.state.feishu_sync_service_ref = service
    return coordinator


def request_knowledge_migration_service(request: Request) -> KnowledgeMigrationService:
    """Resolve migration service without warming HyperRAG upload manager.

    Migration only needs the upload store + storage config; building the full
    KnowledgeUploadManager (and HyperRAG adapter side effects) is unnecessary
    and can block the accept path.
    """
    service = getattr(request.app.state, "knowledge_migration_service", None)
    if isinstance(service, KnowledgeMigrationService):
        return service
    config = request_app_config(request)
    if config.knowledge is None:
        raise HTTPException(status_code=503, detail="knowledge backend is not configured")
    upload_store = getattr(request.app.state, "knowledge_upload_store", None)
    if upload_store is None:
        upload_store = build_knowledge_upload_store(config)
        request.app.state.knowledge_upload_store = upload_store
    service = build_migration_service(config, upload_store)
    request.app.state.knowledge_migration_service = service
    return service


def _validate_library_id(library_id: str) -> str:
    value = (library_id or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="library_id is required")
    if "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid library_id")
    return value


def _validate_asset_id(asset_id: str) -> str:
    value = (asset_id or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="asset_id is required")
    if "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid asset_id")
    return value


def _asset_status_value(status: Any) -> str:
    if isinstance(status, dict):
        raw_status = status.get("status")
    elif isinstance(status, str):
        raw_status = status
    else:
        raw_status = getattr(status, "status", None)
    if isinstance(raw_status, dict):
        raw_status = raw_status.get("value") or raw_status.get("status")
    return str(getattr(raw_status, "value", raw_status) or "").strip().lower()


def _online_index_status(
    status: str,
) -> Literal["queued", "running", "completed", "failed", "cancelled"]:
    if status in {"queued", "pending"}:
        return "queued"
    if status in {"parsing", "chunking", "indexing", "post_processing", "running", "deleting"}:
        return "running"
    if status in {"completed", "done"}:
        return "completed"
    if status in {"failed"}:
        return "failed"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    raise ValueError(f"unsupported asset status: {status or '<empty>'}")


def _migration_job_response(job: MigrationJob) -> MigrationJobResponse:
    return MigrationJobResponse(
        job_id=job.job_id,
        kind=job.kind,
        library_id=job.library_id,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        message=job.message,
        error=job.error,
        result=job.result,
        created_at=datetime.fromtimestamp(job.created_at),
        updated_at=datetime.fromtimestamp(job.updated_at),
    )


def _knowledge_upload_response(record: KnowledgeUploadRecord) -> KnowledgeUploadResponse:
    return KnowledgeUploadResponse(
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


def _ensure_runtime_worker_dependencies(app: FastAPI) -> None:
    if not getattr(app.state, "runtime_tool_configs_ready", False):
        app.state.tool_configs = materialize_runtime_tool_configs(
            app.state.config,
            app.state.tool_configs,
        )
        app.state.runtime_tool_configs_ready = True
    if getattr(app.state, "session_event_store", None) is None:
        app.state.session_event_store = SessionEventStore(app.state.session_store)
    if getattr(app.state, "user_memory_service", None) is None:
        app.state.user_memory_service = build_user_memory_service(app.state.config)


def _ensure_filesystem_manager(app: FastAPI) -> None:
    if getattr(app.state, "filesystem_manager", None) is not None:
        return
    deepagents_config = resolve_deepagents_config(app.state.config)
    app.state.filesystem_manager = FilesystemManager(
        session_store=app.state.session_store,
        workspace_config=deepagents_config.workspace,
    )


def _build_profile_runtime(app: FastAPI, profile: AgentProfile) -> AgentRuntime:
    _ensure_runtime_worker_dependencies(app)
    _ensure_filesystem_manager(app)
    overrides = getattr(app.state, "scheduler_pool_overrides", None) or {}
    return build_agent_runtime(
        app.state.config,
        profile=profile,
        tool_configs=app.state.tool_configs,
        workspace_root=_workspace_root(),
        event_store=app.state.session_event_store,
        knowledge_adapter=app.state.knowledge_adapter,
        user_memory_service=app.state.user_memory_service,
        filesystem_manager=app.state.filesystem_manager,
        scheduler_pool_overrides=dict(overrides),
        multi_agent_enabled=getattr(app.state, "multi_agent_enabled_override", None),
        langfuse_project_identity=app.state.langfuse_project_identity,
    )


def _managed_agent_status(run: RunRecord) -> AgentStatus:
    return AgentStatus(
        status=AgentStatusType(run.status.value),
        run_id=run.id,
        session_id=run.session_id,
        message=run.error,
    )


def _session_profile_unavailable_http_error(
    _exc: SessionProfileUnavailable,
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "session_profile_unavailable",
            "message": "session profile is unavailable",
        },
    )


def _run_submission_failed_http_error(exc: RunSubmissionFailed) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "run_submission_failed",
            "message": "run submission failed",
            "run_id": exc.run_id,
            "session_id": exc.session_id,
            "workspace_id": exc.workspace_id,
            "profile_id": exc.profile_id,
        },
    )


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    return load_app_config()


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _create_session_store(
    config: AppConfig,
    catalog: AgentProfileCatalog,
) -> SessionStore:
    store = build_session_store(config)
    store.migrate_session_profile_ids(
        profile_capabilities=profile_capability_signatures(catalog),
        default_profile_id=catalog.default_profile_id,
    )
    return store


def _reclaim_stale_session_runs_if_leader(store: SessionStore) -> None:
    """Reclaim leftover running runs only while holding the Redis db0 leader lease."""

    if not is_leader_service():
        logger.warning(
            "Skipping stale running-run reclaim; leader lease not held "
            "(must not kill shared-DB in-flight runs)"
        )
        return
    store.mark_stale_running_runs_failed("runtime restarted before run completed")


async def _start_scheduler_metrics(app: FastAPI, config: AppConfig):
    """Start Postgres-backed scheduler metrics when persistence backend is postgres."""

    from viknow.core.scheduler.metrics_collector import SchedulerMetricsCollector
    from viknow.core.scheduler.metrics_events import bind_scheduler_metrics_store
    from viknow.core.scheduler.metrics_store import SchedulerMetricsStore

    if config.persistence.backend != PersistenceBackend.POSTGRES:
        return None
    postgres = config.persistence.postgres
    if postgres is None:
        return None
    store = SchedulerMetricsStore(
        resolve_scheduler_postgres_dsn(config.persistence),
        schema=postgres.scheduler_schema,
    )
    if is_leader_service():
        closed = store.mark_stale_open_run_events("runtime restarted before run completed")
        if closed:
            logger.warning("Marked {} stale scheduler run_events as failed in Postgres", closed)
    else:
        logger.warning("Skipping stale scheduler run_events reclaim; leader lease not held")
    bind_scheduler_metrics_store(store)
    app.state.scheduler_metrics_store = store
    registry = app.state.agent_runtime_registry
    collector = SchedulerMetricsCollector(
        store,
        runtime_items=registry.cached_runtimes,
    )
    await collector.start()
    app.state.scheduler_metrics_collector = collector
    return collector


def _start_usage_push_worker(config: AppConfig):
    """Start the algorithm-side usage push worker when configured.

    Returns ``None`` when disabled or the callback URL is missing, so Q&A is
    unaffected by an unconfigured usage push.
    """
    from viknow.core.usage.worker import start_usage_push_worker

    return start_usage_push_worker(config.observability.langfuse.usage_push)


def _build_learning_projection_service(
    config: AppConfig,
    session_store: SessionStore,
) -> LearningProjectionService | None:
    if not config.learning.enabled:
        return None
    return LearningProjectionService(
        session_store=session_store,
        learning_store=build_learning_projection_store(config),
        max_attempts=config.learning.max_attempts,
    )


def _session_workspace_dir(config: AppConfig, session) -> Path | None:
    return resolve_session_workspace_dir(
        config,
        user_id=session.user_id,
        workspace_id=session.workspace_id,
        session_id=session.id,
    )


async def _parse_create_run_request(
    request: Request,
) -> tuple[CreateRunRequest, list[tuple[str, bytes, str | None]]]:
    """Parse JSON create-run body, or multipart with optional ``files`` uploads."""

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        payload = CreateRunRequest.model_validate(await request.json())
        return payload, []

    form = await request.form()
    raw_session_id = form.get("session_id")
    session_id = None
    if raw_session_id not in (None, ""):
        session_id = UUID(str(raw_session_id))

    attachment_file_ids: list[str] = []
    for item in form.getlist("attachment_file_ids"):
        value = str(item).strip()
        if value:
            attachment_file_ids.append(value)

    library_ids: list[str] | None = None
    if "library_ids" in form:
        library_ids = [str(item).strip() for item in form.getlist("library_ids")]

    raw_profile_id = form.get("profile_id")
    profile_id = str(raw_profile_id).strip() if raw_profile_id not in (None, "") else None
    raw_agent_model = form.get("agent_model")
    agent_model = str(raw_agent_model).strip() if raw_agent_model not in (None, "") else None
    payload = CreateRunRequest(
        user_id=UUID(str(form.get("user_id"))),
        session_id=session_id,
        message=str(form.get("message") or ""),
        profile_id=profile_id or None,
        agent_model=agent_model or None,
        use_precompiled_knowledge=form.get("use_precompiled_knowledge", True),
        attachment_file_ids=attachment_file_ids or None,
        library_ids=library_ids,
    )

    upload_files: list[tuple[str, bytes, str | None]] = []
    for item in form.getlist("files"):
        if not hasattr(item, "read"):
            continue
        content = await item.read()
        upload_files.append(
            (
                getattr(item, "filename", None) or "attachment.bin",
                content,
                getattr(item, "content_type", None),
            )
        )
    return payload, upload_files


def _user_response(user) -> UserResponse:
    return UserResponse(
        id=user.id,
        created_at=user.created_at,
        updated_at=user.updated_at,
        display_name=user.display_name,
        memory_enabled=user.memory_enabled,
    )


def _session_response(session) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        user_id=session.user_id,
        workspace_id=session.workspace_id,
        profile_id=session.profile_id,
        title=session.title,
        status=str(session.status),
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_run_id=session.last_run_id,
        active_run_id=session.active_run_id,
    )
