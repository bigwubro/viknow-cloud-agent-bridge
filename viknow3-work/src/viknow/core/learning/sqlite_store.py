"""SQLite persistence for Learning Projection data and its local outbox."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from viknow.core.learning.projection import LearningFeedback, LearningProjectionRecord
from viknow.core.learning.store import LearningProjectionJob


class SqliteLearningProjectionStore:
    """Content-free learning persistence colocated with a local development state dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(self, *, run_id: str, feedback_revision: int) -> bool:
        now = _dump_datetime(_now())
        key = _job_key(run_id, feedback_revision)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO learning_projection_jobs (
                    key, run_id, feedback_revision, status, attempts, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?)
                """,
                (key, run_id, feedback_revision, now, now),
            )
        return cursor.rowcount == 1

    def claim_pending(self, *, limit: int) -> list[LearningProjectionJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = _dump_datetime(_now())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM learning_projection_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC, key ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            keys = [str(row["key"]) for row in rows]
            if keys:
                conn.executemany(
                    """
                    UPDATE learning_projection_jobs
                    SET status = 'processing', attempts = attempts + 1, updated_at = ?
                    WHERE key = ? AND status = 'pending'
                    """,
                    [(now, key) for key in keys],
                )
            claimed = []
            for row in rows:
                claimed.append(
                    {
                        **dict(row),
                        "status": "processing",
                        "attempts": int(row["attempts"]) + 1,
                        "updated_at": now,
                    }
                )
        return [_job_from_row(row) for row in claimed]

    def mark_job_completed(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE learning_projection_jobs
                SET status = 'completed', last_error = NULL, updated_at = ?
                WHERE key = ?
                """,
                (_dump_datetime(_now()), key),
            )

    def mark_job_failed(self, key: str, *, error: str, give_up: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE learning_projection_jobs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE key = ?
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
        now = _dump_datetime(_now())
        identity = record.sample.projection_identity
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO learning_projections (
                    storage_key, run_id, projection_version, fusion_rule_version, feedback_revision,
                    run_kind, eligible_for_learning, source_event_sequences_json,
                    sample_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.storage_key,
                    identity.run_id,
                    identity.projection_version,
                    identity.fusion_rule_version,
                    identity.feedback_revision,
                    record.sample.run_kind,
                    int(record.sample.eligible_for_learning),
                    json.dumps(record.source_event_sequences),
                    record.sample.model_dump_json(),
                    now,
                ),
            )
        return cursor.rowcount == 1

    def get_projection(self, storage_key: str) -> LearningProjectionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_event_sequences_json, sample_json
                FROM learning_projections WHERE storage_key = ?
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
                )
                WHERE revision_rank = 1
                ORDER BY created_at DESC, storage_key DESC
                LIMIT ?
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
                WHERE run_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            revision = (int(row["revision"]) if row else 0) + 1
            prior = str(row["disposition"]) if row and row["disposition"] != "withdrawn" else None
            conn.execute(
                """
                INSERT INTO learning_feedback (
                    run_id, revision, disposition, prior_disposition, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, revision, disposition, prior, _dump_datetime(_now())),
            )
        return LearningFeedback(
            revision=revision,
            disposition=disposition,
            prior_disposition=prior,
        )

    def feedback_for_revision(self, *, run_id: str, revision: int) -> LearningFeedback | None:
        if revision == 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT revision, disposition, prior_disposition FROM learning_feedback
                WHERE run_id = ? AND revision = ?
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
                    eligible_for_learning INTEGER NOT NULL,
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
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _job_key(run_id: str, feedback_revision: int) -> str:
    return f"{run_id}:feedback-{feedback_revision}"


def _job_from_row(row: sqlite3.Row | dict[str, object]) -> LearningProjectionJob:
    return LearningProjectionJob(
        key=str(row["key"]),
        run_id=str(row["run_id"]),
        feedback_revision=int(row["feedback_revision"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _projection_from_row(row: sqlite3.Row) -> LearningProjectionRecord:
    return LearningProjectionRecord(
        storage_key=str(row["storage_key"]),
        source_event_sequences=json.loads(row["source_event_sequences_json"]),
        sample=json.loads(row["sample_json"]),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _dump_datetime(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
