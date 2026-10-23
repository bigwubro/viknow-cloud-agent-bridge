"""Postgres persistence for Learning Projection records and durable outbox jobs."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from viknow.core.learning.projection import LearningFeedback, LearningProjectionRecord
from viknow.core.learning.sqlite_store import _dump_datetime, _now
from viknow.core.learning.store import LearningProjectionJob
from viknow.core.postgres_pool import connection as pooled_connection


class PostgresLearningProjectionStore:
    """Learning store bound to the configured ``viknow_learning`` search path."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._initialize()

    def enqueue(self, *, run_id: str, feedback_revision: int) -> bool:
        now = _dump_datetime(_now())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO learning_projection_jobs (
                    key, run_id, feedback_revision, status, attempts, last_error,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 'pending', 0, NULL, %s, %s)
                ON CONFLICT (run_id, feedback_revision) DO NOTHING
                """,
                (_job_key(run_id, feedback_revision), run_id, feedback_revision, now, now),
            )
        return cursor.rowcount == 1

    def claim_pending(self, *, limit: int) -> list[LearningProjectionJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = _dump_datetime(_now())
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH claimed AS (
                    SELECT key
                    FROM learning_projection_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC, key ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE learning_projection_jobs AS jobs
                SET status = 'processing', attempts = jobs.attempts + 1, updated_at = %s
                FROM claimed
                WHERE jobs.key = claimed.key
                RETURNING jobs.*
                """,
                (limit, now),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def mark_job_completed(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE learning_projection_jobs
                SET status = 'completed', last_error = NULL, updated_at = %s
                WHERE key = %s
                """,
                (_dump_datetime(_now()), key),
            )

    def mark_job_failed(self, key: str, *, error: str, give_up: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE learning_projection_jobs
                SET status = %s, last_error = %s, updated_at = %s
                WHERE key = %s
                """,
                ("failed" if give_up else "pending", error, _dump_datetime(_now()), key),
            )

    def pending_jobs(self) -> list[LearningProjectionJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM learning_projection_jobs
                WHERE status IN ('pending', 'processing')
                ORDER BY created_at ASC, key ASC
                """
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def save_projection(self, record: LearningProjectionRecord) -> bool:
        identity = record.sample.projection_identity
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO learning_projections (
                    storage_key, run_id, projection_version, fusion_rule_version, feedback_revision,
                    run_kind, eligible_for_learning, source_event_sequences_json,
                    sample_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (storage_key) DO NOTHING
                """,
                (
                    record.storage_key,
                    identity.run_id,
                    identity.projection_version,
                    identity.fusion_rule_version,
                    identity.feedback_revision,
                    record.sample.run_kind,
                    record.sample.eligible_for_learning,
                    json.dumps(record.source_event_sequences),
                    record.sample.model_dump_json(),
                    _dump_datetime(_now()),
                ),
            )
        return cursor.rowcount == 1

    def get_projection(self, storage_key: str) -> LearningProjectionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_event_sequences_json, sample_json
                FROM learning_projections WHERE storage_key = %s
                """,
                (storage_key,),
            ).fetchone()
        if row is None:
            return None
        return LearningProjectionRecord(
            storage_key=storage_key,
            source_event_sequences=json.loads(row["source_event_sequences_json"]),
            sample=json.loads(row["sample_json"]),
        )

    def list_projections(self, *, limit: int) -> list[LearningProjectionRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT storage_key, source_event_sequences_json, sample_json FROM (
                    SELECT storage_key, source_event_sequences_json, sample_json, created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY run_id
                            ORDER BY feedback_revision DESC, created_at DESC, storage_key DESC
                        ) AS revision_rank
                    FROM learning_projections
                ) AS latest
                WHERE revision_rank = 1
                ORDER BY created_at DESC, storage_key DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [_projection_from_row(row) for row in rows]

    def append_feedback(self, *, run_id: str, disposition: str) -> LearningFeedback:
        if disposition not in {"helpful", "unhelpful", "withdrawn"}:
            raise ValueError("unsupported feedback disposition")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT revision, disposition FROM learning_feedback
                WHERE run_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE
                """,
                (run_id,),
            ).fetchone()
            revision = (int(row["revision"]) if row else 0) + 1
            prior = str(row["disposition"]) if row and row["disposition"] != "withdrawn" else None
            conn.execute(
                """
                INSERT INTO learning_feedback (
                    run_id, revision, disposition, prior_disposition, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (run_id, revision, disposition, prior, _dump_datetime(_now())),
            )
        return LearningFeedback(revision=revision, disposition=disposition, prior_disposition=prior)

    def feedback_for_revision(self, *, run_id: str, revision: int) -> LearningFeedback | None:
        if revision == 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT revision, disposition, prior_disposition FROM learning_feedback
                WHERE run_id = %s AND revision = %s
                """,
                (run_id, revision),
            ).fetchone()
        if row is None:
            return None
        return LearningFeedback(
            revision=int(row["revision"]),
            disposition=str(row["disposition"]),
            prior_disposition=row["prior_disposition"],
        )

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_projections (
                    storage_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    projection_version INTEGER NOT NULL,
                    fusion_rule_version TEXT NOT NULL,
                    feedback_revision INTEGER NOT NULL,
                    run_kind TEXT NOT NULL,
                    eligible_for_learning BOOLEAN NOT NULL,
                    source_event_sequences_json TEXT NOT NULL,
                    sample_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, projection_version, fusion_rule_version, feedback_revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_feedback (
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    disposition TEXT NOT NULL,
                    prior_disposition TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_projection_jobs (
                    key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    feedback_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, feedback_revision)
                )
                """
            )
            conn.execute(
                """
                UPDATE learning_projection_jobs
                SET status = 'pending'
                WHERE status = 'processing'
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with pooled_connection(self.dsn) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _job_key(run_id: str, feedback_revision: int) -> str:
    return f"{run_id}:feedback-{feedback_revision}"


def _job_from_row(row: Mapping[str, Any]) -> LearningProjectionJob:
    return LearningProjectionJob(
        key=str(row["key"]),
        run_id=str(row["run_id"]),
        feedback_revision=int(row["feedback_revision"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _projection_from_row(row: Mapping[str, Any]) -> LearningProjectionRecord:
    return LearningProjectionRecord(
        storage_key=str(row["storage_key"]),
        source_event_sequences=json.loads(row["source_event_sequences_json"]),
        sample=json.loads(row["sample_json"]),
    )
