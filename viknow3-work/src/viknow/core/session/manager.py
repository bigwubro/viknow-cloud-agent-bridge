"""Session orchestration around AgentRuntime."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from viknow.core.profiles import AgentProfileCatalog, UnknownProfile
from viknow.core.runtime.domain import (
    AgentEvent,
    AgentEventType,
    AgentOp,
    AgentOpType,
    KnowledgeAssetSelection,
    TraceCaptureMode,
)
from viknow.core.runtime.events import AgentRuntime
from viknow.core.runtime.registry import AgentRuntimeRegistry
from viknow.core.session.domain import (
    CreateRunResult,
    InvalidAttachmentFiles,
    RawModelOutputDebug,
    RunDebug,
    RunNotFound,
    RunRecord,
    RunStatus,
    RunSubmissionFailed,
    SessionBusy,
    SessionDebug,
    SessionFileNotFound,
    SessionFileRecord,
    SessionNotFound,
    SessionProfileConflict,
    SessionProfileUnavailable,
    SessionRecord,
    UserNotFound,
    UserRecord,
    WorkspaceDebug,
    WorkspaceFileDebug,
)
from viknow.core.session.event_utils import payload_text, run_tool_calls
from viknow.core.session.store import SessionStore
from viknow.core.session.timeline import (
    AnswerReviewDetail,
    ReasoningDetail,
    SessionTimeline,
    ToolCallDetail,
    build_session_timeline,
    get_answer_review_detail,
    get_reasoning_detail,
    get_tool_call_detail,
)

if TYPE_CHECKING:
    from viknow.core.filesystem.manager import FilesystemManager

_EXCLUDED_WORKSPACE_DEBUG_DIRS = {"__pycache__", "node_modules"}
_MAX_WORKSPACE_DEBUG_FILES = 200
_MAX_WORKSPACE_DEBUG_SCAN_ENTRIES = 2000
_RUN_SUBMISSION_FAILED_ERROR = "run submission failed"


class SessionManager:
    """Coordinates user/session metadata with the agent runtime."""

    def __init__(
        self,
        *,
        store: SessionStore,
        catalog: AgentProfileCatalog,
        runtime_registry: AgentRuntimeRegistry,
        filesystem_manager: FilesystemManager | Any | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.runtime_registry = runtime_registry
        self.filesystem_manager = filesystem_manager

    def _require_filesystem_manager(self) -> Any:
        if self.filesystem_manager is None:
            raise RuntimeError("filesystem manager is not configured")
        return self.filesystem_manager

    def create_user(self, *, display_name: str | None = None) -> UserRecord:
        return self.store.create_user(display_name=display_name)

    def get_user(self, user_id: str) -> UserRecord:
        user = self.store.get_user(user_id)
        if user is None:
            raise UserNotFound(user_id)
        return user

    def update_user_memory_enabled(self, *, user_id: str, memory_enabled: bool) -> UserRecord:
        self.get_user(user_id)
        try:
            return self.store.update_user_memory_enabled(
                user_id=user_id,
                memory_enabled=memory_enabled,
            )
        except KeyError as exc:
            raise UserNotFound(user_id) from exc

    def list_sessions(self, user_id: str) -> list[SessionRecord]:
        self.get_user(user_id)
        return self.store.list_sessions(user_id)

    def get_session(self, *, user_id: str, session_id: str) -> SessionRecord:
        self.get_user(user_id)
        session = self.store.get_session_for_user(user_id=user_id, session_id=session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def list_session_events(self, *, user_id: str, session_id: str) -> list[AgentEvent]:
        session = self.get_session(user_id=user_id, session_id=session_id)
        return self.store.events_for_session(session.id)

    def session_timeline(self, *, user_id: str, session_id: str) -> SessionTimeline:
        self.get_session(user_id=user_id, session_id=session_id)
        return build_session_timeline(self.store, session_id=session_id)

    def tool_call_detail(
        self,
        *,
        user_id: str,
        session_id: str,
        call_id: str,
    ) -> ToolCallDetail:
        self.get_session(user_id=user_id, session_id=session_id)
        return get_tool_call_detail(self.store, session_id=session_id, call_id=call_id)

    def reasoning_detail(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> ReasoningDetail:
        self.get_session(user_id=user_id, session_id=session_id)
        return get_reasoning_detail(self.store, session_id=session_id, run_id=run_id)

    def answer_review_detail(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> AnswerReviewDetail:
        self.get_session(user_id=user_id, session_id=session_id)
        return get_answer_review_detail(self.store, session_id=session_id, run_id=run_id)

    def delete_session(self, *, user_id: str, session_id: str) -> None:
        session = self.get_session(user_id=user_id, session_id=session_id)
        if self.store.session_has_running_run(session.id):
            raise SessionBusy(session.id)
        if not self.store.soft_delete_session_for_user(user_id=user_id, session_id=session.id):
            raise SessionNotFound(session_id)

    def session_debug(
        self,
        *,
        user_id: str,
        session_id: str,
        workspace_dir: Path | None,
    ) -> SessionDebug:
        session = self.get_session(user_id=user_id, session_id=session_id)
        workspace = self.store.get_workspace(session.workspace_id)
        if workspace is None:
            raise SessionNotFound(session_id)

        events_by_run: defaultdict[str, list[AgentEvent]] = defaultdict(list)
        for event in self.store.events_for_session(session.id):
            events_by_run[event.run_id].append(event)

        runs: list[RunDebug] = []
        for run in self.store.list_runs_for_session(session.id):
            events = events_by_run[run.id]
            event_counts = Counter(event.type for event in events)
            duration_seconds = None
            if run.ended_at is not None:
                duration_seconds = (run.ended_at - run.started_at).total_seconds()
            enabled_tools, enabled_skills = _run_capabilities(events)
            runs.append(
                RunDebug(
                    id=run.id,
                    status=run.status,
                    message=run.message,
                    created_at=run.created_at,
                    started_at=run.started_at,
                    ended_at=run.ended_at,
                    duration_seconds=duration_seconds,
                    error=run.error,
                    event_count=len(events),
                    enabled_tools=enabled_tools,
                    enabled_skills=enabled_skills,
                    tool_call_started_count=event_counts[AgentEventType.TOOL_CALL_STARTED],
                    tool_call_completed_count=event_counts[AgentEventType.TOOL_CALL_COMPLETED],
                    tool_call_failed_count=event_counts[AgentEventType.TOOL_CALL_FAILED],
                    tool_calls=run_tool_calls(events),
                    raw_model_outputs=_run_raw_model_outputs(events),
                )
            )

        return SessionDebug(
            session=session,
            workspace=WorkspaceDebug(
                id=workspace.id,
                name=workspace.name,
                status=workspace.status,
                files=_workspace_files(workspace_dir),
            ),
            runs=runs,
        )

    def get_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    def upload_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_name: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> SessionFileRecord:
        self.get_session(user_id=user_id, session_id=session_id)
        return self._require_filesystem_manager().upload_session_file(
            user_id=user_id,
            session_id=session_id,
            file_name=file_name,
            content=content,
            mime_type=mime_type,
        )

    def list_session_files(self, *, user_id: str, session_id: str) -> list[SessionFileRecord]:
        self.get_session(user_id=user_id, session_id=session_id)
        return self._require_filesystem_manager().list_session_files(
            user_id=user_id,
            session_id=session_id,
        )

    def get_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> SessionFileRecord:
        self.get_session(user_id=user_id, session_id=session_id)
        return self._require_filesystem_manager().get_session_file(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )

    def open_session_file_stream(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ):
        self.get_session(user_id=user_id, session_id=session_id)
        return self._require_filesystem_manager().open_session_file_stream(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )

    def delete_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> None:
        self.get_session(user_id=user_id, session_id=session_id)
        if self.store.session_has_running_run(session_id):
            raise SessionBusy(session_id)
        self._require_filesystem_manager().delete_session_file(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )

    async def create_run(
        self,
        *,
        user_id: str,
        session_id: str | None,
        message: str,
        profile_id: str | None = None,
        agent_model: str | None = None,
        use_precompiled_knowledge: bool = True,
        trace_capture: TraceCaptureMode = TraceCaptureMode.NONE,
        langfuse_trace_id: str | None = None,
        langfuse_trace_source_id: str | None = None,
        langfuse_trace_name: str | None = None,
        attachment_file_ids: list[str] | None = None,
        library_ids: list[str] | None = None,
        upload_files: list[tuple[str, bytes, str | None]] | None = None,
        skill_flow_gold_trace: list[dict[str, Any]] | None = None,
        selected_knowledge_assets: list[KnowledgeAssetSelection] | None = None,
        oracle_scope: dict[str, Any] | None = None,
        oracle_task: dict[str, Any] | None = None,
        on_run_created: Callable[[str], None] | None = None,
    ) -> CreateRunResult:
        user = self.get_user(user_id)
        if session_id is None:
            profile = self.catalog.resolve(profile_id)
            workspace = self.store.create_workspace(user_id=user_id, name="默认工作区")
            session = self.store.create_session(
                user_id=user_id,
                workspace_id=workspace.id,
                title=_title_from_message(message),
                profile_id=profile.id,
            )
        else:
            session = self.store.get_session_for_user(user_id=user_id, session_id=session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if profile_id is not None and profile_id != session.profile_id:
                raise SessionProfileConflict(session.id, session.profile_id, profile_id)
            if self.store.session_has_running_run(session.id):
                raise SessionBusy(session.id)
            self._require_session_profile(session)

        resolved_attachment_ids = list(attachment_file_ids or [])
        if upload_files:
            manager = self._require_filesystem_manager()
            for file_name, content, mime_type in upload_files:
                record = manager.upload_session_file(
                    user_id=user_id,
                    session_id=session.id,
                    file_name=file_name,
                    content=content,
                    mime_type=mime_type,
                )
                resolved_attachment_ids.append(record.id)

        if resolved_attachment_ids:
            manager = self._require_filesystem_manager()
            missing: list[str] = []
            for file_id in resolved_attachment_ids:
                try:
                    manager.get_session_file(
                        user_id=user_id,
                        session_id=session.id,
                        file_id=file_id,
                    )
                except SessionFileNotFound:
                    missing.append(file_id)
            if missing:
                raise InvalidAttachmentFiles(missing)

        run = self.store.create_run(
            session_id=session.id,
            user_id=user_id,
            workspace_id=session.workspace_id,
            message=message,
        )
        if on_run_created is not None:
            on_run_created(run.id)
        try:
            runtime = await self.runtime_registry.get(session.profile_id)
            await runtime.submit(
                AgentOp(
                    type=AgentOpType.USER_INPUT,
                    input=message,
                    run_id=run.id,
                    session_id=session.id,
                    user_id=user_id,
                    workspace_id=session.workspace_id,
                    agent_model=agent_model,
                    trace_capture=trace_capture,
                    langfuse_trace_id=langfuse_trace_id,
                    langfuse_trace_source_id=langfuse_trace_source_id,
                    langfuse_trace_name=langfuse_trace_name,
                    memory_enabled=user.memory_enabled,
                    use_precompiled_knowledge=use_precompiled_knowledge,
                    attachment_file_ids=resolved_attachment_ids or None,
                    library_ids=library_ids,
                    skill_flow_gold_trace=skill_flow_gold_trace,
                    selected_knowledge_assets=selected_knowledge_assets or [],
                    oracle_scope=oracle_scope,
                    oracle_task=oracle_task,
                )
            )
        except Exception as exc:
            self.store.mark_run_terminal(
                run.id,
                RunStatus.FAILED,
                error=_RUN_SUBMISSION_FAILED_ERROR,
            )
            raise RunSubmissionFailed(
                run_id=run.id,
                session_id=session.id,
                workspace_id=session.workspace_id,
                profile_id=session.profile_id,
            ) from exc
        self.runtime_registry.bind_run(run.id, runtime)
        return CreateRunResult(
            run_id=run.id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            profile_id=session.profile_id,
        )

    async def runtime_for_run(self, run_id: str) -> AgentRuntime:
        """Resolve a Run Runtime from its persisted Session Profile binding."""

        run = self.get_run(run_id)
        session = self.store.get_session(run.session_id)
        if session is None:
            raise SessionNotFound(run.session_id)
        self._require_session_profile(session)
        bound = self.runtime_registry.runtime_for_run_id(run_id)
        if bound is not None:
            return bound
        return await self.runtime_registry.get(session.profile_id)

    def _require_session_profile(self, session: SessionRecord) -> None:
        try:
            self.catalog.require(session.profile_id)
        except UnknownProfile as exc:
            raise SessionProfileUnavailable(session.id, session.profile_id) from exc


def _title_from_message(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:40] or "新会话"


def _run_capabilities(events: list[AgentEvent]) -> tuple[list[str], list[str]]:
    for event in events:
        if event.type == AgentEventType.RUN_STARTED:
            return (
                _string_list(event.payload.get("enabled_tools")),
                _string_list(event.payload.get("enabled_skills")),
            )
    return [], []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _run_raw_model_outputs(events: list[AgentEvent]) -> list[RawModelOutputDebug]:
    outputs: list[RawModelOutputDebug] = []
    for event in events:
        if event.type != AgentEventType.MESSAGE_COMPLETED:
            continue
        raw_output = event.payload.get("raw_model_output")
        if not isinstance(raw_output, str) or not raw_output:
            continue
        outputs.append(
            RawModelOutputDebug(
                sequence=event.sequence,
                source=payload_text(event.payload, "raw_model_output_source") or "runtime",
                stage=payload_text(event.payload, "raw_model_output_stage") or "final_output",
                payload_json=raw_output,
            )
        )
    return outputs


def _workspace_files(workspace_dir: Path | None) -> list[WorkspaceFileDebug]:
    if (
        workspace_dir is None
        or workspace_dir.is_symlink()
        or not workspace_dir.exists()
        or not workspace_dir.is_dir()
    ):
        return []

    files: list[WorkspaceFileDebug] = []
    stack = [workspace_dir]
    scanned_entries = 0
    while stack and len(files) < _MAX_WORKSPACE_DEBUG_FILES:
        directory = stack.pop()
        try:
            entries = directory.iterdir()
        except OSError:
            continue

        for path in entries:
            scanned_entries += 1
            if scanned_entries > _MAX_WORKSPACE_DEBUG_SCAN_ENTRIES:
                return sorted(files, key=lambda item: item.path)
            if path.is_symlink():
                continue
            try:
                relative = path.relative_to(workspace_dir)
            except ValueError:
                continue
            if _is_excluded_workspace_debug_path(relative):
                continue
            if path.is_dir():
                stack.append(path)
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append(
                WorkspaceFileDebug(
                    path=relative.as_posix(),
                    size_bytes=stat.st_size,
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
            if len(files) >= _MAX_WORKSPACE_DEBUG_FILES:
                break
    return sorted(files, key=lambda item: item.path)


def _is_excluded_workspace_debug_path(path: Path) -> bool:
    return any(
        part.startswith(".") or part in _EXCLUDED_WORKSPACE_DEBUG_DIRS for part in path.parts
    )
