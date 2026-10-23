"""Postgres persistence for ViKnow user and session metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import psycopg

from viknow.core.postgres_pool import connection as pooled_connection
from viknow.core.profiles import CapabilitySignature
from viknow.core.runtime.domain import AgentEvent
from viknow.core.session.domain import (
    RunRecord,
    RunStatus,
    SessionFileRecord,
    SessionRecord,
    SessionStatus,
    UserRecord,
    WorkspaceRecord,
    WorkspaceStatus,
)
from viknow.core.session.event_policy import EPHEMERAL_EVENT_TYPES
from viknow.core.session.mapping import (
    capability_signature,
    dump_dt,
    run_from_row,
    session_file_from_row,
    session_from_row,
    user_from_row,
    workspace_from_row,
)
from viknow.core.session.mapping import (
    now as utc_now,
)


class PostgresSessionStore:
    """Postgres-backed session metadata store."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._initialize()

    def create_user(self, *, display_name: str | None = None) -> UserRecord:
        now = utc_now()
        record = UserRecord(
            id=str(uuid4()),
            created_at=now,
            updated_at=now,
            display_name=display_name,
            memory_enabled=False,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, created_at, updated_at, display_name, memory_enabled)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    dump_dt(record.created_at),
                    dump_dt(record.updated_at),
                    display_name,
                    record.memory_enabled,
                ),
            )
        return record

    def update_user_memory_enabled(self, *, user_id: str, memory_enabled: bool) -> UserRecord:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE users
                SET memory_enabled = %s, updated_at = %s
                WHERE id = %s
                """,
                (memory_enabled, dump_dt(now), user_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(user_id)
        user = self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return user_from_row(row) if row is not None else None

    def create_workspace(self, *, user_id: str, name: str) -> WorkspaceRecord:
        now = utc_now()
        record = WorkspaceRecord(
            id=str(uuid4()),
            user_id=user_id,
            name=name,
            status=WorkspaceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspaces (id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.user_id,
                    record.name,
                    record.status,
                    dump_dt(record.created_at),
                    dump_dt(record.updated_at),
                ),
            )
        return record

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = %s", (workspace_id,)).fetchone()
        return workspace_from_row(row) if row is not None else None

    def create_session(
        self,
        *,
        user_id: str,
        workspace_id: str,
        title: str,
        profile_id: str,
    ) -> SessionRecord:
        if not profile_id.strip():
            raise ValueError("profile_id must not be blank")
        now = utc_now()
        record = SessionRecord(
            id=str(uuid4()),
            user_id=user_id,
            workspace_id=workspace_id,
            profile_id=profile_id,
            title=title,
            status=SessionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, workspace_id, profile_id, title, status,
                    created_at, updated_at, last_run_id, active_run_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.user_id,
                    record.workspace_id,
                    record.profile_id,
                    record.title,
                    record.status,
                    dump_dt(record.created_at),
                    dump_dt(record.updated_at),
                    record.last_run_id,
                    record.active_run_id,
                ),
            )
        return record

    def migrate_session_profile_ids(
        self,
        *,
        profile_capabilities: Mapping[str, tuple[CapabilitySignature, ...]],
        default_profile_id: str,
    ) -> int:
        """Backfill null Session Profiles from the latest persisted capability snapshot."""

        changed = 0
        with self._connect() as conn:
            sessions = conn.execute("SELECT id FROM sessions WHERE profile_id IS NULL").fetchall()
            for session in sessions:
                event_row = conn.execute(
                    """
                    SELECT events.payload_json
                    FROM runs
                    JOIN events ON events.run_id = runs.id
                    WHERE runs.session_id = %s AND events.type = 'run_started'
                    ORDER BY runs.created_at DESC, runs.ctid DESC, events.sequence DESC
                    LIMIT 1
                    """,
                    (session["id"],),
                ).fetchone()
                signature = (
                    capability_signature(event_row["payload_json"])
                    if event_row is not None
                    else None
                )
                matches = (
                    [
                        profile_id
                        for profile_id, accepted in profile_capabilities.items()
                        if signature in accepted
                    ]
                    if signature is not None
                    else []
                )
                resolved = matches[0] if len(matches) == 1 else default_profile_id
                cursor = conn.execute(
                    """
                    UPDATE sessions
                    SET profile_id = %s
                    WHERE id = %s AND profile_id IS NULL
                    """,
                    (resolved, session["id"]),
                )
                changed += cursor.rowcount
        return changed

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        return _session_record_from_row(row) if row is not None else None

    def get_session_for_user(self, *, user_id: str, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = %s AND user_id = %s AND status = %s",
                (session_id, user_id, SessionStatus.ACTIVE),
            ).fetchone()
        return _session_record_from_row(row) if row is not None else None

    def list_sessions(self, user_id: str) -> list[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = %s AND status = %s
                ORDER BY updated_at DESC, created_at DESC
                """,
                (user_id, SessionStatus.ACTIVE),
            ).fetchall()
        return [_session_record_from_row(row) for row in rows]

    def soft_delete_session_for_user(self, *, user_id: str, session_id: str) -> bool:
        now = utc_now()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE sessions
                SET status = %s, updated_at = %s, active_run_id = NULL
                WHERE id = %s AND user_id = %s AND status = %s
                """,
                (
                    SessionStatus.DELETED,
                    dump_dt(now),
                    session_id,
                    user_id,
                    SessionStatus.ACTIVE,
                ),
            )
        return result.rowcount > 0

    def create_run(
        self,
        *,
        session_id: str,
        user_id: str,
        workspace_id: str,
        message: str,
        run_id: str | None = None,
    ) -> RunRecord:
        now = utc_now()
        record = RunRecord(
            id=run_id or str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            message=message,
            created_at=now,
            started_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, session_id, user_id, workspace_id, status, message,
                    created_at, started_at, ended_at, error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.session_id,
                    record.user_id,
                    record.workspace_id,
                    record.status,
                    record.message,
                    dump_dt(record.created_at),
                    dump_dt(record.started_at),
                    None,
                    None,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET updated_at = %s, last_run_id = %s, active_run_id = %s
                WHERE id = %s
                """,
                (dump_dt(now), record.id, record.id, session_id),
            )
            conn.execute(
                "UPDATE workspaces SET updated_at = %s WHERE id = %s",
                (dump_dt(now), workspace_id),
            )
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        return run_from_row(row) if row is not None else None

    def list_runs_for_session(self, session_id: str) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE session_id = %s
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [run_from_row(row) for row in rows]

    def session_has_running_run(self, session_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE session_id = %s AND status = %s
                LIMIT 1
                """,
                (session_id, RunStatus.RUNNING),
            ).fetchone()
        return row is not None

    def append_event(self, event: AgentEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, sequence, type, payload_json, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id, sequence) DO UPDATE SET
                    type = EXCLUDED.type,
                    payload_json = EXCLUDED.payload_json,
                    created_at = EXCLUDED.created_at
                """,
                (
                    event.run_id,
                    event.sequence,
                    event.type,
                    event.model_dump_json(),
                    dump_dt(utc_now()),
                ),
            )

    def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM events
                WHERE run_id = %s AND sequence > %s
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [AgentEvent.model_validate_json(row["payload_json"]) for row in rows]

    def events_for_session(self, session_id: str) -> list[AgentEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT events.payload_json FROM events
                JOIN runs ON runs.id = events.run_id
                WHERE runs.session_id = %s
                ORDER BY runs.created_at ASC, events.sequence ASC
                """,
                (session_id,),
            ).fetchall()
        events = [AgentEvent.model_validate_json(row["payload_json"]) for row in rows]
        return [event for event in events if event.type not in EPHEMERAL_EVENT_TYPES]

    def mark_run_terminal(self, run_id: str, status: RunStatus, *, error: str | None) -> None:
        now = utc_now()
        with self._connect() as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
            if run_row is None:
                return
            conn.execute(
                """
                UPDATE runs
                SET status = %s, ended_at = %s, error = %s
                WHERE id = %s
                """,
                (status, dump_dt(now), error, run_id),
            )
            conn.execute(
                """
                UPDATE sessions
                SET updated_at = %s, last_run_id = %s, active_run_id = NULL
                WHERE id = %s
                """,
                (dump_dt(now), run_id, run_row["session_id"]),
            )
            conn.execute(
                "UPDATE workspaces SET updated_at = %s WHERE id = %s",
                (dump_dt(now), run_row["workspace_id"]),
            )

    def mark_stale_running_runs_failed(self, message: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status = %s",
                (RunStatus.RUNNING,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = %s, ended_at = %s, error = %s
                    WHERE id = %s
                    """,
                    (RunStatus.FAILED, dump_dt(now), message, row["id"]),
                )
            conn.execute(
                """
                UPDATE sessions
                SET active_run_id = NULL, updated_at = %s
                WHERE active_run_id IS NOT NULL
                """,
                (dump_dt(now),),
            )

    def add_session_file(self, record: SessionFileRecord) -> SessionFileRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_files (
                    id, session_id, user_id, workspace_id, file_name, mime_type,
                    size_bytes, kind, status, object_key, virtual_path,
                    uploaded_at, deleted_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.id,
                    record.session_id,
                    record.user_id,
                    record.workspace_id,
                    record.file_name,
                    record.mime_type,
                    record.size_bytes,
                    record.kind.value,
                    record.status.value,
                    record.object_key,
                    record.virtual_path,
                    dump_dt(record.uploaded_at),
                    dump_dt(record.deleted_at) if record.deleted_at else None,
                ),
            )
        return record

    def get_session_file(self, file_id: str) -> SessionFileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_files WHERE id = %s",
                (file_id,),
            ).fetchone()
        return session_file_from_row(row) if row is not None else None

    def get_session_file_for_user(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> SessionFileRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM session_files
                WHERE id = %s AND user_id = %s AND session_id = %s AND deleted_at IS NULL
                """,
                (file_id, user_id, session_id),
            ).fetchone()
        return session_file_from_row(row) if row is not None else None

    def list_session_files(
        self,
        *,
        user_id: str,
        session_id: str,
        include_deleted: bool = False,
    ) -> list[SessionFileRecord]:
        query = """
            SELECT * FROM session_files
            WHERE user_id = %s AND session_id = %s
        """
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY uploaded_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, (user_id, session_id)).fetchall()
        return [session_file_from_row(row) for row in rows]

    def soft_delete_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE session_files
                SET deleted_at = %s
                WHERE id = %s AND user_id = %s AND session_id = %s AND deleted_at IS NULL
                """,
                (dump_dt(utc_now()), file_id, user_id, session_id),
            )
            return cursor.rowcount > 0

    def update_session_file(self, record: SessionFileRecord) -> SessionFileRecord:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE session_files
                SET file_name = %s, mime_type = %s, size_bytes = %s, kind = %s, status = %s,
                    object_key = %s, virtual_path = %s, deleted_at = %s
                WHERE id = %s
                """,
                (
                    record.file_name,
                    record.mime_type,
                    record.size_bytes,
                    record.kind.value,
                    record.status.value,
                    record.object_key,
                    record.virtual_path,
                    dump_dt(record.deleted_at) if record.deleted_at else None,
                    record.id,
                ),
            )
        return record

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    display_name TEXT,
                    memory_enabled BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    profile_id TEXT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_id TEXT,
                    active_run_id TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    error TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                    ON sessions(user_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_session_created
                    ON runs(session_id, created_at ASC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_session_status
                    ON runs(session_id, status)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence
                    ON events(run_id, sequence)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_files (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    mime_type TEXT,
                    size_bytes INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    virtual_path TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_files_session
                    ON session_files(session_id, uploaded_at ASC)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        with pooled_connection(self.dsn) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _session_record_from_row(row: Mapping[str, Any]) -> SessionRecord:
    """Map a session row; coerce NULL profile_id for SessionRecord(str)."""

    profile_id = row["profile_id"]
    if profile_id is None:
        return session_from_row({**dict(row), "profile_id": ""})
    return session_from_row(row)
