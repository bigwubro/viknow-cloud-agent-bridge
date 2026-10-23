"""SQLite persistence for Feishu synchronization state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from viknow.core.knowledge.feishu.domain import (
    FeishuSyncObject,
    FeishuSyncRun,
    FeishuSyncRunStatus,
    FeishuSyncSource,
)
from viknow.core.session.mapping import dump_dt


class SqliteFeishuSyncStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def try_source_run_lock(self, _source_id: str) -> Iterator[bool]:
        yield True

    def upsert_source(self, source: FeishuSyncSource) -> FeishuSyncSource:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feishu_sync_sources (id, schedule_enabled, next_run_at, record_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    schedule_enabled = excluded.schedule_enabled,
                    next_run_at = excluded.next_run_at,
                    record_json = excluded.record_json
                """,
                (
                    source.id,
                    int(source.schedule_enabled),
                    dump_dt(source.next_run_at) if source.next_run_at else None,
                    source.model_dump_json(),
                ),
            )
        return source

    def get_source(self, source_id: str) -> FeishuSyncSource | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feishu_sync_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
        return FeishuSyncSource.model_validate_json(row["record_json"]) if row else None

    def list_sources(self) -> list[FeishuSyncSource]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM feishu_sync_sources ORDER BY id"
            ).fetchall()
        return [FeishuSyncSource.model_validate_json(row["record_json"]) for row in rows]

    def list_due_sources(self, at: datetime) -> list[FeishuSyncSource]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feishu_sync_sources
                WHERE schedule_enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at, id
                """,
                (dump_dt(at),),
            ).fetchall()
        return [FeishuSyncSource.model_validate_json(row["record_json"]) for row in rows]

    def upsert_object(self, item: FeishuSyncObject) -> FeishuSyncObject:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feishu_sync_objects (source_id, node_token, record_json)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id, node_token) DO UPDATE SET
                    record_json = excluded.record_json
                """,
                (item.source_id, item.node_token, item.model_dump_json()),
            )
        return item

    def get_object(self, source_id: str, node_token: str) -> FeishuSyncObject | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM feishu_sync_objects
                WHERE source_id = ? AND node_token = ?
                """,
                (source_id, node_token),
            ).fetchone()
        return FeishuSyncObject.model_validate_json(row["record_json"]) if row else None

    def list_objects(self, source_id: str) -> list[FeishuSyncObject]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feishu_sync_objects
                WHERE source_id = ? ORDER BY node_token
                """,
                (source_id,),
            ).fetchall()
        return [FeishuSyncObject.model_validate_json(row["record_json"]) for row in rows]

    def delete_object(self, source_id: str, node_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM feishu_sync_objects WHERE source_id = ? AND node_token = ?",
                (source_id, node_token),
            )
        return cursor.rowcount > 0

    def add_run(self, run: FeishuSyncRun) -> FeishuSyncRun:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feishu_sync_runs (id, source_id, status, started_at, record_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.source_id,
                    run.status.value,
                    dump_dt(run.started_at),
                    run.model_dump_json(),
                ),
            )
        return run

    def update_run(self, run: FeishuSyncRun) -> FeishuSyncRun:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE feishu_sync_runs SET status = ?, record_json = ? WHERE id = ?
                """,
                (run.status.value, run.model_dump_json(), run.id),
            )
        return run

    def get_run(self, run_id: str) -> FeishuSyncRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feishu_sync_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return FeishuSyncRun.model_validate_json(row["record_json"]) if row else None

    def list_runs(self, source_id: str | None = None, *, limit: int = 50) -> list[FeishuSyncRun]:
        query = "SELECT record_json FROM feishu_sync_runs"
        params: tuple[object, ...]
        if source_id is None:
            params = (limit,)
        else:
            query += " WHERE source_id = ?"
            params = (source_id, limit)
        query += " ORDER BY started_at DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [FeishuSyncRun.model_validate_json(row["record_json"]) for row in rows]

    def list_active_runs(self) -> list[FeishuSyncRun]:
        terminal = (
            FeishuSyncRunStatus.COMPLETED.value,
            FeishuSyncRunStatus.PARTIAL_FAILED.value,
            FeishuSyncRunStatus.FAILED.value,
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feishu_sync_runs
                WHERE status NOT IN (?, ?, ?) ORDER BY started_at
                """,
                terminal,
            ).fetchall()
        return [FeishuSyncRun.model_validate_json(row["record_json"]) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS feishu_sync_sources (
                    id TEXT PRIMARY KEY,
                    schedule_enabled INTEGER NOT NULL,
                    next_run_at TEXT,
                    record_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feishu_sync_sources_due
                    ON feishu_sync_sources(schedule_enabled, next_run_at);

                CREATE TABLE IF NOT EXISTS feishu_sync_objects (
                    source_id TEXT NOT NULL,
                    node_token TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (source_id, node_token)
                );

                CREATE TABLE IF NOT EXISTS feishu_sync_runs (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feishu_sync_runs_source_started
                    ON feishu_sync_runs(source_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_feishu_sync_runs_status
                    ON feishu_sync_runs(status);
                """
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


__all__ = ["SqliteFeishuSyncStore"]
