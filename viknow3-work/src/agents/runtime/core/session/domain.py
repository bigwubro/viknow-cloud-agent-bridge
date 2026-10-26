"""User, workspace, session, and run domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceStatus(StrEnum):
    """Workspace lifecycle values."""

    ACTIVE = "active"


class SessionStatus(StrEnum):
    """Session lifecycle values."""

    ACTIVE = "active"
    DELETED = "deleted"


class RunStatus(StrEnum):
    """Agent run lifecycle values."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class SessionError(Exception):
    """Base exception for session management failures."""


class UserNotFound(SessionError):
    """Raised when a requested user does not exist."""


class SessionNotFound(SessionError):
    """Raised when a session does not exist for the current user."""


class RunNotFound(SessionError):
    """Raised when a run does not exist."""


class ToolCallNotFound(SessionError):
    """Raised when a tool call does not exist in the requested session."""


class SessionBusy(SessionError):
    """Raised when a session already has a running run."""


class SessionProfileConflict(SessionError):
    """Raised when a request attempts to change an existing Session Profile."""

    def __init__(self, session_id: str, current: str, requested: str) -> None:
        super().__init__(f"session {session_id} is bound to profile {current}")
        self.session_id = session_id
        self.current_profile_id = current
        self.requested_profile_id = requested


class SessionProfileUnavailable(SessionError):
    """Raised when a persisted Session Profile is absent from this deployment."""

    def __init__(self, session_id: str, profile_id: str) -> None:
        super().__init__(f"profile {profile_id} is unavailable for session {session_id}")
        self.session_id = session_id
        self.profile_id = profile_id


class RunSubmissionFailed(SessionError):
    """Raised after a persisted Run fails during Runtime initialization or submit."""

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        workspace_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(f"failed to submit persisted run {run_id}")
        self.run_id = run_id
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.profile_id = profile_id


class SessionFileNotFound(SessionError):
    """Raised when a session file does not exist or is deleted."""


class SessionFileTooLarge(SessionError):
    """Raised when an upload exceeds the configured size limit."""

    def __init__(self, *, max_file_mb: int) -> None:
        super().__init__(f"file exceeds max size of {max_file_mb} MB")
        self.max_file_mb = max_file_mb


class InvalidAttachmentFiles(SessionError):
    """Raised when attachment_file_ids are missing or not owned by the session."""

    def __init__(self, file_ids: list[str]) -> None:
        super().__init__(f"invalid attachment file ids: {', '.join(file_ids)}")
        self.file_ids = file_ids


class SessionFileKind(StrEnum):
    """Logical role of a session file."""

    UPLOAD = "upload"
    OUTPUT = "output"
    DERIVATIVE = "derivative"


class SessionFileStatus(StrEnum):
    """Lifecycle status for a session file."""

    UPLOADED = "uploaded"
    PARSED = "parsed"
    FAILED = "failed"


class SessionFileRecord(BaseModel):
    """Persisted metadata for one session workspace file.

    Bytes live on the files-root path tree.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    user_id: str
    workspace_id: str
    file_name: str
    mime_type: str | None = None
    size_bytes: int
    kind: SessionFileKind = SessionFileKind.UPLOAD
    status: SessionFileStatus = SessionFileStatus.UPLOADED
    object_key: str
    virtual_path: str
    uploaded_at: datetime
    deleted_at: datetime | None = None


class UserRecord(BaseModel):
    """Persisted lightweight user identity."""

    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: datetime
    updated_at: datetime
    display_name: str | None = None
    memory_enabled: bool = False


class WorkspaceRecord(BaseModel):
    """Persisted workspace metadata."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    name: str
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime


class SessionRecord(BaseModel):
    """Persisted conversation session metadata."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    workspace_id: str
    profile_id: str
    title: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_run_id: str | None = None
    active_run_id: str | None = None


class RunRecord(BaseModel):
    """Persisted metadata for one agent execution."""

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    user_id: str
    workspace_id: str
    status: RunStatus
    message: str
    created_at: datetime
    started_at: datetime
    ended_at: datetime | None = None
    error: str | None = None


class CreateRunResult(BaseModel):
    """Result returned after creating a managed run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    workspace_id: str
    profile_id: str


class WorkspaceFileDebug(BaseModel):
    """Read-only workspace file metadata for diagnostics."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    updated_at: datetime


class WorkspaceDebug(BaseModel):
    """Workspace diagnostics exposed to the web debug drawer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: WorkspaceStatus
    files: list[WorkspaceFileDebug]


class ToolCallDebug(BaseModel):
    """Tool call details exposed to the web debug drawer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    status: str
    arguments: str = ""
    result: str | None = None
    error: str | None = None
    agent: str | None = None
    agent_name: str | None = None


class RawModelOutputDebug(BaseModel):
    """Raw agent/model output captured for diagnostics."""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    source: str
    stage: str
    payload_json: str


class RunDebug(BaseModel):
    """Run diagnostics for one session run."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: RunStatus
    message: str
    created_at: datetime
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    error: str | None = None
    event_count: int = 0
    enabled_tools: list[str] = Field(default_factory=list)
    enabled_skills: list[str] = Field(default_factory=list)
    tool_call_started_count: int = 0
    tool_call_completed_count: int = 0
    tool_call_failed_count: int = 0
    tool_calls: list[ToolCallDebug] = Field(default_factory=list)
    raw_model_outputs: list[RawModelOutputDebug] = Field(default_factory=list)


class SessionDebug(BaseModel):
    """Current session diagnostics for the web debug drawer."""

    model_config = ConfigDict(extra="forbid")

    session: SessionRecord
    workspace: WorkspaceDebug
    runs: list[RunDebug]
