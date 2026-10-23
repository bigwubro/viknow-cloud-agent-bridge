"""PostgreSQL persistence for Feishu synchronization state."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from viknow.core.knowledge.feishu.domain import (
    FeishuSyncObject,
    FeishuSyncRun,
    FeishuSyncRunStatus,
    FeishuSyncSource,
)
from viknow.core.postgres_pool import connection as pooled_connection
from viknow.core.session.mapping import dump_dt


class PostgresFeishuSyncStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._initialize()

    @contextmanager
    def try_source_run_lock(self, source_id: str) -> Iterator[bool]:
        """Hold a process-independent lock for one source without persisting state."""
        lock_id = _source_run_lock_id(source_id)
        with pooled_connection(self.dsn) as connection:
            acquired = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(%s) AS acquired",
                    (lock_id,),
                ).fetchone()["acquired"]
            )
            connection.commit()
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    released = bool(
                        connection.execute(
                            "SELECT pg_advisory_unlock(%s) AS released",
                            (lock_id,),
                        ).fetchone()["released"]
                    )
                    connection.commit()
                except Exception:
                    connection.close()
                    raise
                if not released:
                    connection.close()

    def upsert_source(self, source: FeishuSyncSource) -> FeishuSyncSource:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feishu_sync_sources (id, schedule_enabled, next_run_at, record_json)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    schedule_enabled = excluded.schedule_enabled,
                    next_run_at = excluded.next_run_at,
                    record_json = excluded.record_json
                """,
                (
                    source.id,
                    source.schedule_enabled,
                    dump_dt(source.next_run_at) if source.next_run_at else None,
                    source.model_dump_json(),
                ),
            )
        return source

    def get_source(self, source_id: str) -> FeishuSyncSource | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feishu_sync_sources WHERE id = %s",
                (source_id,),
            ).fetchone()
        return self._source(row)

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
                WHERE schedule_enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= %s
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
                VALUES (%s, %s, %s)
                ON CONFLICT(source_id, node_token) DO UPDATE SET record_json = excluded.record_json
                """,
                (item.source_id, item.node_token, item.model_dump_json()),
            )
        return item

    def get_object(self, source_id: str, node_token: str) -> FeishuSyncObject | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM feishu_sync_objects
                WHERE source_id = %s AND node_token = %s
                """,
                (source_id, node_token),
            ).fetchone()
        return FeishuSyncObject.model_validate_json(row["record_json"]) if row else None

    def list_objects(self, source_id: str) -> list[FeishuSyncObject]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM feishu_sync_objects
                WHERE source_id = %s ORDER BY node_token
                """,
                (source_id,),
            ).fetchall()
        return [FeishuSyncObject.model_validate_json(row["record_json"]) for row in rows]

    def delete_object(self, source_id: str, node_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM feishu_sync_objects WHERE source_id = %s AND node_token = %s",
                (source_id, node_token),
            )
        return cursor.rowcount > 0

    def add_run(self, run: FeishuSyncRun) -> FeishuSyncRun:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feishu_sync_runs (id, source_id, status, started_at, record_json)
                VALUES (%s, %s, %s, %s, %s)
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
                "UPDATE feishu_sync_runs SET status = %s, record_json = %s WHERE id = %s",
                (run.status.value, run.model_dump_json(), run.id),
            )
        return run

    def get_run(self, run_id: str) -> FeishuSyncRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM feishu_sync_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
        return FeishuSyncRun.model_validate_json(row["record_json"]) if row else None

    def list_runs(self, source_id: str | None = None, *, limit: int = 50) -> list[FeishuSyncRun]:
        query = "SELECT record_json FROM feishu_sync_runs"
        params: tuple[object, ...]
        if source_id is None:
            params = (limit,)
        else:
            query += " WHERE source_id = %s"
            params = (source_id, limit)
        query += " ORDER BY started_at DESC LIMIT %s"
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
                WHERE status NOT IN (%s, %s, %s) ORDER BY started_at
                """,
                terminal,
            ).fetchall()
        return [FeishuSyncRun.model_validate_json(row["record_json"]) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_sync_sources (
                    id TEXT PRIMARY KEY,
                    schedule_enabled BOOLEAN NOT NULL,
                    next_run_at TEXT,
                    record_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feishu_sync_sources_due
                    ON feishu_sync_sources(schedule_enabled, next_run_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_sync_objects (
                    source_id TEXT NOT NULL,
                    node_token TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (source_id, node_token)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_sync_runs (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feishu_sync_runs_source_started
                    ON feishu_sync_runs(source_id, started_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feishu_sync_runs_status
                    ON feishu_sync_runs(status)
                """
            )

    @contextmanager
    def _connect(self):
        with pooled_connection(self.dsn) as connection:
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _source(row: dict[str, Any] | None) -> FeishuSyncSource | None:
        return FeishuSyncSource.model_validate_json(row["record_json"]) if row else None


def _source_run_lock_id(source_id: str) -> int:
    digest = hashlib.blake2b(
        f"viknow:feishu-sync:{source_id}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


__all__ = ["PostgresFeishuSyncStore"]
