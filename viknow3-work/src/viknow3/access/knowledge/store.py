"""Persisted index jobs and HyperRAG status reads (no catalog mirror of viknow-storage)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from viknow3.clock import now, stamp
from viknow3.pg import CATALOG_SCHEMA, HYPERRAG, HYPERRAG_SCHEMA, connect

_CONN = connect(HYPERRAG, schema=CATALOG_SCHEMA)


def _index_job_from_row(row: Any) -> IndexJobRow:
    if isinstance(row, (tuple, list)):
        parts = tuple(row)
    else:
        parts = tuple(row[i] for i in range(7))
    return IndexJobRow(*parts)


def _drop_catalog_mirror() -> None:
    """Remove legacy knowledge_libraries / knowledge_assets and catalog FKs."""
    jobs = f"{CATALOG_SCHEMA}.knowledge_index_jobs"
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            ALTER TABLE IF EXISTS {HYPERRAG_SCHEMA}.hyperrag_asset_status
            DROP CONSTRAINT IF EXISTS hyperrag_asset_status_asset_fk
            """
        )
        cur.execute("SELECT to_regclass(%s)", (jobs,))
        if cur.fetchone()[0] is not None:
            cur.execute(
                f"""
                DO $viknow$
                DECLARE constraint_name text;
                BEGIN
                  FOR constraint_name IN
                    SELECT c.conname
                    FROM pg_constraint c
                    WHERE c.contype = 'f'
                      AND c.conrelid = '{jobs}'::regclass
                  LOOP
                    EXECUTE format(
                      'ALTER TABLE {CATALOG_SCHEMA}.knowledge_index_jobs DROP CONSTRAINT %I',
                      constraint_name
                    );
                  END LOOP;
                END
                $viknow$
                """
            )
        cur.execute(f"DROP TABLE IF EXISTS {CATALOG_SCHEMA}.knowledge_assets")
        cur.execute(f"DROP TABLE IF EXISTS {CATALOG_SCHEMA}.knowledge_libraries")


def _ensure() -> None:
    _drop_catalog_mirror()
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CATALOG_SCHEMA}.knowledge_index_jobs (
                job_id text PRIMARY KEY,
                library_id text NOT NULL,
                asset_id text NOT NULL,
                status text NOT NULL,
                error_message text,
                created_at timestamptz NOT NULL,
                completed_at timestamptz
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_index_jobs_asset_latest
                ON {CATALOG_SCHEMA}.knowledge_index_jobs (library_id, asset_id, created_at DESC);
            """
        )


_ensure()


def storage_uri(library_id: str, asset_id: str) -> str:
    return f"storage://{library_id}/{asset_id}"


def get_latest_hyperrag_asset_status(library_id: str, asset_id: str) -> dict[str, Any] | None:
    with _CONN.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            (f"{HYPERRAG_SCHEMA}.hyperrag_asset_status",),
        )
        if cur.fetchone()[0] is None:
            return None
        cur.execute(
            f"""
            SELECT workspace, id, asset_id, status, error_message, updated_at
            FROM {HYPERRAG_SCHEMA}.hyperrag_asset_status
            WHERE workspace = %s AND asset_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (library_id, asset_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "workspace": row[0],
        "id": row[1],
        "asset_id": row[2],
        "status": row[3],
        "error_message": row[4],
        "updated_at": row[5],
    }


@dataclass
class IndexJobRow:
    job_id: str
    library_id: str
    asset_id: str
    status: str
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "library_id": self.library_id,
            "asset_id": self.asset_id,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": stamp(self.created_at),
            "completed_at": stamp(self.completed_at) if self.completed_at else None,
        }


def _parse_completed_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return None


def insert_index_job(
    library_id: str,
    asset_id: str,
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
) -> IndexJobRow:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    job_id = str(job_id or "").strip()
    status = str(status or "").strip() or "pending"
    if not library_id or not asset_id or not job_id:
        raise ValueError("invalid_index_job")
    ts = now()
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {CATALOG_SCHEMA}.knowledge_index_jobs (
                job_id, library_id, asset_id, status, error_message, created_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NULL)
            ON CONFLICT (job_id) DO UPDATE SET
                library_id = EXCLUDED.library_id,
                asset_id = EXCLUDED.asset_id,
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                created_at = EXCLUDED.created_at,
                completed_at = NULL
            """,
            (job_id, library_id, asset_id, status, error_message, ts),
        )
    return IndexJobRow(job_id, library_id, asset_id, status, error_message, ts, None)


def update_index_job(
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    job_id = str(job_id or "").strip()
    status = str(status or "").strip()
    if not job_id or not status:
        raise ValueError("invalid_index_job")
    terminal = status.lower() in {"done", "completed", "failed", "cancelled"}
    completed = completed_at if completed_at is not None else (now() if terminal else None)
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {CATALOG_SCHEMA}.knowledge_index_jobs
            SET status = %s,
                error_message = %s,
                completed_at = COALESCE(%s, completed_at)
            WHERE job_id = %s
            """,
            (status, error_message, completed, job_id),
        )


def get_index_job_record(job_id: str) -> IndexJobRow | None:
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            SELECT job_id, library_id, asset_id, status, error_message, created_at, completed_at
            FROM {CATALOG_SCHEMA}.knowledge_index_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    return None if row is None else _index_job_from_row(row)


def get_latest_index_job(library_id: str, asset_id: str) -> IndexJobRow | None:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        return None
    with _CONN.cursor() as cur:
        cur.execute(
            f"""
            SELECT job_id, library_id, asset_id, status, error_message, created_at, completed_at
            FROM {CATALOG_SCHEMA}.knowledge_index_jobs
            WHERE library_id = %s AND asset_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (library_id, asset_id),
        )
        row = cur.fetchone()
    return None if row is None else _index_job_from_row(row)


def upsert_index_job_snapshot(
    library_id: str,
    asset_id: str,
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
    completed_at: Any = None,
) -> None:
    """Insert or update one job row (HyperRAG poll / asset_status sync)."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return
    existing = get_index_job_record(job_id)
    if existing is None:
        insert_index_job(library_id, asset_id, job_id, status, error_message=error_message)
    else:
        update_index_job(
            job_id,
            status,
            error_message=error_message,
            completed_at=_parse_completed_at(completed_at),
        )
