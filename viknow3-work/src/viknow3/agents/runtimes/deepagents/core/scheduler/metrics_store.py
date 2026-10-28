"""PostgreSQL persistence for ResourceScheduler metrics."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from viknow3.agents.runtimes.deepagents.core.postgres_pool import connection as pooled_connection
from viknow3.agents.runtimes.deepagents.core.scheduler.metrics_domain import (
    MetricsSnapshot,
    PoolGauge,
    ProfileGauge,
    SchedulerWindowStats,
    ToolEvent,
    ToolEventKind,
    ToolKindStats,
    aggregate_run_durations,
    utc_now,
)

RETENTION_DAYS = 7


class SchedulerMetricsStore:
    """Postgres-backed store for scheduler gauge snapshots and run/tool events."""

    def __init__(self, dsn: str, *, schema: str | None = None) -> None:
        self.dsn = dsn
        self.schema = schema
        self._initialize()

    def insert_snapshot(self, snapshot: MetricsSnapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO metrics_snapshots (id, ts, payload)
                VALUES (%s, %s, %s)
                """,
                (str(uuid4()), snapshot.ts, Jsonb(payload)),
            )

    def insert_run_started(
        self,
        *,
        run_id: str,
        profile_id: str,
        started_at: datetime | None = None,
    ) -> None:
        started = started_at or utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, profile_id, started_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET profile_id = EXCLUDED.profile_id,
                    started_at = EXCLUDED.started_at
                """,
                (run_id, profile_id, started),
            )

    def insert_run_finished(
        self,
        *,
        run_id: str,
        profile_id: str,
        started_at: datetime,
        finished_at: datetime | None = None,
        status: str = "completed",
    ) -> None:
        ended = finished_at or utc_now()
        duration_ms = max(0, int((ended - started_at).total_seconds() * 1000))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (
                    run_id, profile_id, started_at, finished_at, duration_ms, status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET profile_id = EXCLUDED.profile_id,
                    finished_at = EXCLUDED.finished_at,
                    duration_ms = EXCLUDED.duration_ms,
                    status = EXCLUDED.status
                """,
                (run_id, profile_id, started_at, ended, duration_ms, status),
            )

    def mark_stale_open_run_events(self, message: str) -> int:
        """Close run_events left open after a process crash or restart."""
        ended = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                UPDATE run_events
                SET finished_at = %s,
                    duration_ms = GREATEST(
                        0,
                        CAST(EXTRACT(EPOCH FROM (%s - started_at)) * 1000 AS BIGINT)
                    ),
                    status = 'failed'
                WHERE finished_at IS NULL
                RETURNING run_id
                """,
                (ended, ended),
            ).fetchall()
        return len(rows)

    def insert_tool_event(self, event: ToolEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_events (id, tool_id, kind, profile_id, run_id, ts)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    event.tool_id,
                    event.kind.value,
                    event.profile_id,
                    event.run_id,
                    event.ts,
                ),
            )

    def latest_snapshot(
        self,
        *,
        profile_id: str | None = None,
    ) -> MetricsSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload
                FROM metrics_snapshots
                ORDER BY ts DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        snapshot = _snapshot_from_payload(row["payload"])
        if profile_id is None:
            return snapshot
        filtered = [item for item in snapshot.profiles if item.profile_id == profile_id]
        return MetricsSnapshot.from_profile_gauges(ts=snapshot.ts, profiles=filtered)

    def list_snapshots(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        profile_id: str | None = None,
    ) -> list[MetricsSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM metrics_snapshots
                WHERE ts >= %s AND ts <= %s
                ORDER BY ts ASC
                """,
                (started_at, ended_at),
            ).fetchall()
        snapshots: list[MetricsSnapshot] = []
        for row in rows:
            snapshot = _snapshot_from_payload(row["payload"])
            if profile_id is None:
                snapshots.append(snapshot)
                continue
            filtered = [item for item in snapshot.profiles if item.profile_id == profile_id]
            snapshots.append(MetricsSnapshot.from_profile_gauges(ts=snapshot.ts, profiles=filtered))
        return snapshots

    def window_stats(
        self,
        *,
        started_at: datetime,
        ended_at: datetime,
        profile_id: str | None = None,
    ) -> SchedulerWindowStats:
        with self._connect() as conn:
            run_params: list[Any] = [started_at, ended_at]
            run_filter = ""
            if profile_id is not None:
                run_filter = " AND profile_id = %s"
                run_params.append(profile_id)
            run_rows = conn.execute(
                f"""
                SELECT duration_ms
                FROM run_events
                WHERE finished_at IS NOT NULL
                  AND finished_at >= %s
                  AND finished_at <= %s
                  {run_filter}
                """,
                tuple(run_params),
            ).fetchall()

            tool_params: list[Any] = [started_at, ended_at]
            tool_filter = ""
            if profile_id is not None:
                tool_filter = " AND profile_id = %s"
                tool_params.append(profile_id)
            tool_rows = conn.execute(
                f"""
                SELECT tool_id, kind
                FROM tool_events
                WHERE ts >= %s AND ts <= %s
                  {tool_filter}
                """,
                tuple(tool_params),
            ).fetchall()

        durations = [int(row["duration_ms"]) for row in run_rows if row["duration_ms"] is not None]
        timeout_count = sum(1 for row in tool_rows if row["kind"] == ToolEventKind.TIMEOUT)
        failure_count = sum(1 for row in tool_rows if row["kind"] == ToolEventKind.FAILURE)
        by_tool: dict[str, dict[str, int]] = {}
        for row in tool_rows:
            bucket = by_tool.setdefault(str(row["tool_id"]), {"timeout": 0, "failure": 0})
            if row["kind"] == ToolEventKind.TIMEOUT:
                bucket["timeout"] += 1
            else:
                bucket["failure"] += 1
        return SchedulerWindowStats(
            runs=aggregate_run_durations(durations),
            tools=ToolKindStats(
                timeout_count=timeout_count,
                failure_count=failure_count,
                by_tool=by_tool,
            ),
        )

    def delete_older_than(self, *, days: int = RETENTION_DAYS) -> int:
        cutoff = utc_now() - timedelta(days=days)
        with self._connect() as conn:
            deleted_snapshots = conn.execute(
                "DELETE FROM metrics_snapshots WHERE ts < %s",
                (cutoff,),
            ).rowcount
            deleted_runs = conn.execute(
                """
                DELETE FROM run_events
                WHERE COALESCE(finished_at, started_at) < %s
                """,
                (cutoff,),
            ).rowcount
            deleted_tools = conn.execute(
                "DELETE FROM tool_events WHERE ts < %s",
                (cutoff,),
            ).rowcount
        return int(deleted_snapshots) + int(deleted_runs) + int(deleted_tools)

    def _initialize(self) -> None:
        with self._connect() as conn:
            if self.schema:
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    id TEXT PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_metrics_snapshots_ts
                    ON metrics_snapshots (ts DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    finished_at TIMESTAMPTZ,
                    duration_ms INTEGER,
                    status TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_run_events_finished
                    ON run_events (finished_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_events (
                    id TEXT PRIMARY KEY,
                    tool_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    profile_id TEXT,
                    run_id TEXT,
                    ts TIMESTAMPTZ NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tool_events_ts
                    ON tool_events (ts)
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


def _snapshot_from_payload(payload: Any) -> MetricsSnapshot:
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    profiles = [
        ProfileGauge(
            profile_id=str(item.get("profile_id") or ""),
            active_runs=int(item.get("active_runs") or 0),
            load=float(item.get("load") or 0.0),
            pools=[
                PoolGauge.model_validate(pool) if isinstance(pool, dict) else pool
                for pool in item.get("pools") or []
            ],
        )
        for item in data.get("profiles") or []
    ]
    ts_raw = data.get("ts")
    ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
    assert isinstance(ts, datetime)
    return MetricsSnapshot(
        ts=ts,
        profiles=profiles,
        active_runs=int(data.get("active_runs") or sum(p.active_runs for p in profiles)),
        load=data.get("load"),
    )


__all__ = ["RETENTION_DAYS", "SchedulerMetricsStore"]
