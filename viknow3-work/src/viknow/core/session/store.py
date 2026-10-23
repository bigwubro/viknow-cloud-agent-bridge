"""Session persistence protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from viknow.core.profiles import CapabilitySignature
from viknow.core.runtime.domain import AgentEvent
from viknow.core.session.domain import (
    RunRecord,
    RunStatus,
    SessionFileRecord,
    SessionRecord,
    UserRecord,
    WorkspaceRecord,
)


class SessionStore(Protocol):
    """Persistence boundary for users, sessions, runs, and events."""

    def create_user(self, *, display_name: str | None = None) -> UserRecord: ...

    def get_user(self, user_id: str) -> UserRecord | None: ...

    def update_user_memory_enabled(self, *, user_id: str, memory_enabled: bool) -> UserRecord: ...

    def create_workspace(self, *, user_id: str, name: str) -> WorkspaceRecord: ...

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None: ...

    def create_session(
        self,
        *,
        user_id: str,
        workspace_id: str,
        title: str,
        profile_id: str,
    ) -> SessionRecord: ...

    def migrate_session_profile_ids(
        self,
        *,
        profile_capabilities: Mapping[str, tuple[CapabilitySignature, ...]],
        default_profile_id: str,
    ) -> int: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def get_session_for_user(self, *, user_id: str, session_id: str) -> SessionRecord | None: ...

    def list_sessions(self, user_id: str) -> list[SessionRecord]: ...

    def soft_delete_session_for_user(self, *, user_id: str, session_id: str) -> bool: ...

    def create_run(
        self,
        *,
        session_id: str,
        user_id: str,
        workspace_id: str,
        message: str,
        run_id: str | None = None,
    ) -> RunRecord: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def list_runs_for_session(self, session_id: str) -> list[RunRecord]: ...

    def session_has_running_run(self, session_id: str) -> bool: ...

    def append_event(self, event: AgentEvent) -> None: ...

    def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]: ...

    def events_for_session(self, session_id: str) -> list[AgentEvent]: ...

    def mark_run_terminal(self, run_id: str, status: RunStatus, *, error: str | None) -> None: ...

    def mark_stale_running_runs_failed(self, message: str) -> None: ...

    def add_session_file(self, record: SessionFileRecord) -> SessionFileRecord: ...

    def get_session_file(self, file_id: str) -> SessionFileRecord | None: ...

    def get_session_file_for_user(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> SessionFileRecord | None: ...

    def list_session_files(
        self,
        *,
        user_id: str,
        session_id: str,
        include_deleted: bool = False,
    ) -> list[SessionFileRecord]: ...

    def soft_delete_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> bool: ...

    def update_session_file(self, record: SessionFileRecord) -> SessionFileRecord: ...
