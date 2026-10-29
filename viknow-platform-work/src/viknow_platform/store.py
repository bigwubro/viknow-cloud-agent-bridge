"""Access-layer state in the viknow_access database.

Users, sessions, workspaces, files, runs, run events, and run files live here,
so deleting a session or a user is one transaction. The LangGraph checkpoint
(the agent's memory of a session) and index jobs live in their own databases.

Deleting only writes deleted_at. Rows with deleted_at set are treated as absent.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from psycopg.types.json import Json

from viknow_platform.clock import new_id, now, stamp
from viknow_platform.pg import ACCESS, connect, require_test_database

MAX_FILE_BYTES = 20 * 1024 * 1024


class Row:
    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name.startswith("_"):
            return
        try:
            persist = object.__getattribute__(self, "_persist")
        except AttributeError:
            return
        if persist:
            object.__getattribute__(self, "_write")()

    def __post_init__(self) -> None:
        object.__setattr__(self, "_persist", True)

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    def update(self, **values: Any) -> None:
        """Set several fields with one write."""
        object.__setattr__(self, "_persist", False)
        try:
            for name, value in values.items():
                setattr(self, name, value)
        finally:
            object.__setattr__(self, "_persist", True)
        self._write()


class PgDict(dict):
    def __setitem__(self, key: str, value: Row) -> None:
        dict.__setitem__(self, key, value)
        value._write()


@dataclass
class User(Row):
    id: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    memory_enabled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
            "memory_enabled": self.memory_enabled,
        }

    def _write(self) -> None:
        _exec(
            """
            INSERT INTO users (id, display_name, created_at, updated_at, deleted_at, memory_enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                updated_at = EXCLUDED.updated_at,
                deleted_at = EXCLUDED.deleted_at,
                memory_enabled = EXCLUDED.memory_enabled
            """,
            (self.id, self.display_name, self.created_at, self.updated_at, self.deleted_at, self.memory_enabled),
        )


@dataclass
class FileRec(Row):
    id: str
    workspace_id: str
    session_id: str
    agent_id: str
    file_name: str
    mime_type: str | None
    size_bytes: int
    kind: str
    status: str
    content: bytes
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "status": self.status,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }

    def _write(self) -> None:
        _exec(
            """
            INSERT INTO files (
                id, workspace_id, session_id, agent_id, file_name, mime_type,
                size_bytes, kind, status, content, created_at, updated_at, deleted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                content = EXCLUDED.content,
                updated_at = EXCLUDED.updated_at,
                deleted_at = EXCLUDED.deleted_at
            """,
            (
                self.id,
                self.workspace_id,
                self.session_id,
                self.agent_id,
                self.file_name,
                self.mime_type,
                self.size_bytes,
                self.kind,
                self.status,
                self.content,
                self.created_at,
                self.updated_at,
                self.deleted_at,
            ),
        )


@dataclass
class Workspace(Row):
    id: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }

    def _write(self) -> None:
        _exec(
            """
            INSERT INTO workspaces (id, session_id, created_at, updated_at, deleted_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                updated_at = EXCLUDED.updated_at,
                deleted_at = EXCLUDED.deleted_at
            """,
            (self.id, self.session_id, self.created_at, self.updated_at, self.deleted_at),
        )


@dataclass
class Session(Row):
    id: str
    user_id: str
    agent_id: str
    title: str
    workspace_id: str
    library_ids: list[str]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "title": self.title,
            "library_ids": list(self.library_ids),
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }

    def _write(self) -> None:
        _exec(
            """
            INSERT INTO sessions (
                id, user_id, agent_id, title, workspace_id, library_ids, created_at, updated_at, deleted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                library_ids = EXCLUDED.library_ids,
                updated_at = EXCLUDED.updated_at,
                deleted_at = EXCLUDED.deleted_at
            """,
            (
                self.id,
                self.user_id,
                self.agent_id,
                self.title,
                self.workspace_id,
                Json(list(self.library_ids)),
                self.created_at,
                self.updated_at,
                self.deleted_at,
            ),
        )


@dataclass
class RunRef(Row):
    id: str
    session_id: str
    user_id: str
    agent_id: str
    file_ids: list[str]
    status: str
    created_at: datetime
    input: str = ""
    output: str | None = None
    model: str | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    citations: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        updated = self.updated_at or self.created_at
        return {
            "id": self.id,
            "session_id": self.session_id,
            "input": self.input,
            "model": self.model,
            "file_ids": list(self.file_ids),
            "status": self.status,
            "output": self.output,
            "citations": dict(self.citations),
            "token_usage": self.token_usage,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(updated),
        }

    def _write(self) -> None:
        updated = self.updated_at or self.created_at
        _exec(
            """
            INSERT INTO run_refs (
                id, session_id, user_id, agent_id, file_ids, status, created_at,
                input, output, model, updated_at, deleted_at, citations, token_usage
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                output = EXCLUDED.output,
                model = EXCLUDED.model,
                updated_at = EXCLUDED.updated_at,
                deleted_at = EXCLUDED.deleted_at,
                citations = EXCLUDED.citations,
                token_usage = EXCLUDED.token_usage
            """,
            (
                self.id,
                self.session_id,
                self.user_id,
                self.agent_id,
                Json(list(self.file_ids)),
                self.status,
                self.created_at,
                self.input,
                self.output,
                self.model,
                updated,
                self.deleted_at,
                Json(dict(self.citations)),
                None if self.token_usage is None else Json(self.token_usage),
            ),
        )


def _exec(sql: str, params: tuple) -> None:
    with _CONN.cursor() as cur:
        cur.execute(sql, params)


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    with _CONN.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


SOFT_DELETE_TABLES = ("users", "workspaces", "sessions", "files", "run_refs")


def _ensure() -> None:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id text PRIMARY KEY,
                display_name text,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz,
                memory_enabled boolean NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                id text PRIMARY KEY,
                session_id text NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id text PRIMARY KEY,
                user_id text NOT NULL,
                agent_id text NOT NULL,
                title text NOT NULL,
                workspace_id text NOT NULL,
                library_ids jsonb NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz
            );
            CREATE TABLE IF NOT EXISTS files (
                id text PRIMARY KEY,
                workspace_id text NOT NULL,
                session_id text NOT NULL,
                agent_id text NOT NULL,
                file_name text NOT NULL,
                mime_type text,
                size_bytes integer NOT NULL,
                kind text NOT NULL,
                status text NOT NULL,
                content bytea NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz
            );
            CREATE TABLE IF NOT EXISTS run_refs (
                id text PRIMARY KEY,
                session_id text NOT NULL,
                user_id text NOT NULL,
                agent_id text NOT NULL,
                file_ids jsonb NOT NULL,
                status text NOT NULL,
                created_at timestamptz NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_events (
                run_id text NOT NULL,
                sequence integer NOT NULL,
                type text NOT NULL,
                payload jsonb NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_files (
                run_id text NOT NULL,
                file_id text NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted_at timestamptz,
                PRIMARY KEY (run_id, file_id)
            );
            """
        )
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS input text NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS output text")
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS model text")
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS updated_at timestamptz")
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS citations jsonb NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE run_refs ADD COLUMN IF NOT EXISTS token_usage jsonb")
        cur.execute("UPDATE run_refs SET updated_at = created_at WHERE updated_at IS NULL")
        for table in SOFT_DELETE_TABLES:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = 'deleted'",
                (table,),
            )
            if cur.fetchone():
                cur.execute(f"UPDATE {table} SET deleted_at = COALESCE(updated_at, created_at) WHERE deleted AND deleted_at IS NULL")
                cur.execute(f"ALTER TABLE {table} DROP COLUMN deleted")


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users: PgDict = PgDict()
        self.sessions: PgDict = PgDict()
        self.workspaces: PgDict = PgDict()
        self.files: PgDict = PgDict()
        self.runs: PgDict = PgDict()
        self._load()

    def _load(self) -> None:
        self.users.clear()
        self.sessions.clear()
        self.workspaces.clear()
        self.files.clear()
        self.runs.clear()
        for row in _rows("SELECT id, display_name, created_at, updated_at, deleted_at, memory_enabled FROM users"):
            user = User(*row)
            dict.__setitem__(self.users, user.id, user)
        for row in _rows("SELECT id, session_id, created_at, updated_at, deleted_at FROM workspaces"):
            item = Workspace(*row)
            dict.__setitem__(self.workspaces, item.id, item)
        for row in _rows(
            "SELECT id, user_id, agent_id, title, workspace_id, library_ids, created_at, updated_at, deleted_at FROM sessions"
        ):
            item = Session(*row[:5], list(row[5]), *row[6:])
            dict.__setitem__(self.sessions, item.id, item)
        for row in _rows(
            """
            SELECT id, workspace_id, session_id, agent_id, file_name, mime_type, size_bytes,
                   kind, status, content, created_at, updated_at, deleted_at
            FROM files
            """
        ):
            content = bytes(row[9])
            item = FileRec(*row[:9], content, *row[10:])
            dict.__setitem__(self.files, item.id, item)
        for row in _rows(
            """
            SELECT id, session_id, user_id, agent_id, file_ids, status, created_at,
                   COALESCE(input, ''), output, model, updated_at, deleted_at, citations, token_usage
            FROM run_refs
            """
        ):
            item = RunRef(
                id=row[0],
                session_id=row[1],
                user_id=row[2],
                agent_id=row[3],
                file_ids=list(row[4]),
                status=row[5],
                created_at=row[6],
                input=row[7],
                output=row[8],
                model=row[9],
                updated_at=row[10],
                deleted_at=row[11],
                citations=dict(row[12] or {}),
                token_usage=row[13],
            )
            dict.__setitem__(self.runs, item.id, item)

    def reset(self) -> None:
        require_test_database(ACCESS)
        with _CONN.cursor() as cur:
            cur.execute("TRUNCATE run_events, run_files, run_refs, files, sessions, workspaces, users")
        self._load()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """One transaction on the shared connection. On failure the cache is reloaded from the database."""
        with self.lock:
            try:
                with _CONN.transaction():
                    yield
            except BaseException:
                self._load()
                raise

    def create_user(self, display_name: str | None) -> User:
        with self.lock:
            moment = now()
            user = User(new_id(), display_name, moment, moment)
            self.users[user.id] = user
            return user

    def get_user(self, user_id: str) -> User | None:
        user = self.users.get(user_id)
        if user is None or user.deleted:
            return None
        return user

    def list_users(self, limit: int, after: str | None) -> list[User]:
        rows = [item for item in self.users.values() if not item.deleted]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return _page(rows, limit, after)

    def update_user(self, user: User, display_name: str | None) -> None:
        with self.lock:
            user.update(display_name=display_name, updated_at=now())

    def delete_user(self, user: User) -> str | None:
        with self.lock:
            if self._user_busy(user.id):
                return "session_busy"
            moment = now()
            with self.atomic():
                user.update(deleted_at=moment, updated_at=moment)
                for session in list(self.sessions.values()):
                    if session.user_id == user.id and not session.deleted:
                        self._delete_session_rows(session, moment)
            return None

    def find_session(self, session_id: str, user_id: str | None = None, agent_id: str | None = None) -> Session | None:
        session = self.sessions.get(session_id)
        if session is None or session.deleted:
            return None
        if user_id is not None and session.user_id != user_id:
            return None
        if agent_id is not None and session.agent_id != agent_id:
            return None
        return session

    def delete_session(self, user_id: str, session_id: str, agent_id: str | None = None) -> str | None:
        """None when deleted, "missing" when absent or not owned, "session_busy" while a run is running."""
        with self.lock:
            session = self.find_session(session_id, user_id, agent_id)
            if session is None:
                return "missing"
            if self._session_busy(session.id):
                return "session_busy"
            with self.atomic():
                self._delete_session_rows(session, now())
            return None

    def delete_run(self, run: RunRef) -> str | None:
        """None when deleted, "run_busy" while it is running. The session and other runs stay."""
        with self.lock:
            if run.status == "running":
                return "run_busy"
            moment = now()
            with self.atomic():
                run.update(deleted_at=moment, updated_at=moment)
                self._delete_run_children([run.id], moment)
            return None

    def _delete_session_rows(self, session: Session, moment: datetime) -> None:
        session.update(deleted_at=moment, updated_at=moment)
        workspace = self.workspaces.get(session.workspace_id)
        if workspace is not None and not workspace.deleted:
            workspace.update(deleted_at=moment, updated_at=moment)
        for record in self.files.values():
            if record.workspace_id == session.workspace_id and not record.deleted:
                record.update(deleted_at=moment, updated_at=moment)
        run_ids = []
        for run in self.runs.values():
            if run.session_id == session.id and not run.deleted:
                run.update(deleted_at=moment, updated_at=moment)
                run_ids.append(run.id)
        self._delete_run_children(run_ids, moment)

    def _delete_run_children(self, run_ids: list[str], moment: datetime) -> None:
        if not run_ids:
            return
        for table in ("run_events", "run_files"):
            _exec(
                f"UPDATE {table} SET deleted_at = %s, updated_at = %s WHERE run_id = ANY(%s) AND deleted_at IS NULL",
                (moment, moment, run_ids),
            )

    def add_run_files(self, run_id: str, file_ids: list[str], moment: datetime) -> None:
        for file_id in dict.fromkeys(file_ids):
            _exec(
                """
                INSERT INTO run_files (run_id, file_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (run_id, file_id, moment, moment),
            )

    def run_file_ids(self, run_id: str) -> list[str]:
        rows = _rows(
            "SELECT file_id FROM run_files WHERE run_id = %s AND deleted_at IS NULL ORDER BY created_at, file_id",
            (run_id,),
        )
        return [row[0] for row in rows]

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        moment: datetime,
        finish: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one event. With finish, the run's final fields are written in the same transaction."""
        with self.atomic():
            sequence = _rows("SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = %s", (run_id,))[0][0]
            _exec(
                """
                INSERT INTO run_events (run_id, sequence, type, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, sequence, event_type, Json(payload), moment, moment),
            )
            if finish is not None:
                self._finish_run(run_id, finish, moment)
        return _event_public(run_id, sequence, event_type, payload, moment, moment)

    def _finish_run(self, run_id: str, finish: dict[str, Any], moment: datetime) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        run.update(
            status=finish["status"],
            output=finish.get("output"),
            citations=dict(finish.get("citations") or {}),
            token_usage=finish.get("token_usage"),
            updated_at=moment,
        )
        session = self.sessions.get(run.session_id)
        if session is not None and not session.deleted:
            session.update(updated_at=moment)

    def read_events(self, run_id: str, after_sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = _rows(
                """
                SELECT sequence, type, payload, created_at, updated_at FROM run_events
                WHERE run_id = %s AND sequence > %s AND deleted_at IS NULL
                ORDER BY sequence
                """,
                (run_id, after_sequence),
            )
        return [_event_public(run_id, *row) for row in rows]

    def fail_runs(self, run_ids: list[str], message: str) -> list[str]:
        """Close runs that are marked running but have no task left, as failed with code run_lost."""
        closed = []
        with self.lock:
            for run_id in run_ids:
                run = self.runs.get(run_id)
                if run is None or run.deleted or run.status != "running":
                    continue
                self.append_event(
                    run.id,
                    "run_failed",
                    {"session_id": run.session_id, "message": message, "code": "run_lost"},
                    now(),
                    finish={"status": "failed", "output": run.output, "citations": run.citations, "token_usage": run.token_usage},
                )
                closed.append(run.id)
        return closed

    def _user_busy(self, user_id: str) -> bool:
        return any(run.status == "running" and run.user_id == user_id and not run.deleted for run in self.runs.values())

    def _session_busy(self, session_id: str) -> bool:
        return any(run.status == "running" and run.session_id == session_id and not run.deleted for run in self.runs.values())


def _event_public(
    run_id: str, sequence: int, event_type: str, payload: dict[str, Any], created_at: datetime, updated_at: datetime
) -> dict[str, Any]:
    return {
        "type": event_type,
        "run_id": run_id,
        "sequence": sequence,
        "created_at": stamp(created_at),
        "updated_at": stamp(updated_at),
        "payload": payload,
    }


def _page(rows: list[Any], limit: int, after: str | None) -> list[Any]:
    if after:
        index = next((i for i, item in enumerate(rows) if item.id == after), None)
        rows = [] if index is None else rows[index + 1 :]
    return rows[:limit]


_CONN = connect(ACCESS)
_ensure()
STORE = Store()
