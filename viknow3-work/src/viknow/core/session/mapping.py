"""Shared row/event mapping helpers for session store backends."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from viknow.core.profiles import CapabilitySignature
from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.session.domain import (
    RunRecord,
    RunStatus,
    SessionFileKind,
    SessionFileRecord,
    SessionFileStatus,
    SessionRecord,
    SessionStatus,
    UserRecord,
    WorkspaceRecord,
    WorkspaceStatus,
)


def now() -> datetime:
    return datetime.now(UTC)


def dump_dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def load_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def user_from_row(row: Mapping[str, Any]) -> UserRecord:
    return UserRecord(
        id=row["id"],
        created_at=load_dt(row["created_at"]),
        updated_at=load_dt(row["updated_at"]),
        display_name=row["display_name"],
        memory_enabled=bool(row["memory_enabled"]),
    )


def workspace_from_row(row: Mapping[str, Any]) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        status=WorkspaceStatus(row["status"]),
        created_at=load_dt(row["created_at"]),
        updated_at=load_dt(row["updated_at"]),
    )


def session_from_row(row: Mapping[str, Any]) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        user_id=row["user_id"],
        workspace_id=row["workspace_id"],
        profile_id=row["profile_id"],
        title=row["title"],
        status=SessionStatus(row["status"]),
        created_at=load_dt(row["created_at"]),
        updated_at=load_dt(row["updated_at"]),
        last_run_id=row["last_run_id"],
        active_run_id=row["active_run_id"],
    )


def session_file_from_row(row: Mapping[str, Any]) -> SessionFileRecord:
    return SessionFileRecord(
        id=row["id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        workspace_id=row["workspace_id"],
        file_name=row["file_name"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        kind=SessionFileKind(row["kind"]),
        status=SessionFileStatus(row["status"]),
        object_key=row["object_key"],
        virtual_path=row["virtual_path"],
        uploaded_at=load_dt(row["uploaded_at"]),
        deleted_at=load_dt(row["deleted_at"]),
    )


def run_from_row(row: Mapping[str, Any]) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        workspace_id=row["workspace_id"],
        status=RunStatus(row["status"]),
        message=row["message"],
        created_at=load_dt(row["created_at"]),
        started_at=load_dt(row["started_at"]),
        ended_at=load_dt(row["ended_at"]),
        error=row["error"],
    )


def capability_signature(payload_json: str) -> CapabilitySignature | None:
    try:
        event = AgentEvent.model_validate_json(payload_json)
    except (TypeError, ValueError):
        return None
    if event.type != AgentEventType.RUN_STARTED:
        return None
    enabled_tools = event.payload.get("enabled_tools")
    enabled_skills = event.payload.get("enabled_skills")
    if not isinstance(enabled_tools, list) or not isinstance(enabled_skills, list):
        return None
    if not all(isinstance(item, str) for item in (*enabled_tools, *enabled_skills)):
        return None
    return frozenset(enabled_tools), frozenset(enabled_skills)
