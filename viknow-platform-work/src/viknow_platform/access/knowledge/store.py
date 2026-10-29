"""Knowledge libraries and assets in Postgres (VIKNOW_HYPERRAG_DSN → viknow_knowledge)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from viknow_platform.clock import now, stamp
from viknow_platform.pg import HYPERRAG, connect

_CONN = connect(HYPERRAG)


def _row_tuple(row: Any, expected: int) -> tuple[Any, ...]:
    if isinstance(row, (tuple, list)):
        return tuple(row)
    return tuple(row[i] for i in range(expected))


def _library_from_row(row: Any) -> LibraryRow:
    parts = _row_tuple(row, 5)
    if len(parts) == 4:
        return LibraryRow(parts[0], parts[1], parts[2], parts[3], False)
    return LibraryRow(parts[0], parts[1], parts[2], parts[3], bool(parts[4]))


def _asset_from_row(row: Any) -> AssetRow:
    parts = _row_tuple(row, 7)
    return AssetRow(*parts)


def _index_job_from_row(row: Any) -> IndexJobRow:
    parts = _row_tuple(row, 7)
    return IndexJobRow(*parts)


def _column_exists(table: str, column: str) -> bool:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            """,
            ("public", table, column),
        )
        return cur.fetchone() is not None


def _migrate_legacy_app_key() -> None:
    if not _column_exists("knowledge_libraries", "app_key"):
        return
    with _CONN.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_libraries_new (
                library_id text PRIMARY KEY,
                title text NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted boolean NOT NULL DEFAULT false
            );
            INSERT INTO knowledge_libraries_new (library_id, title, created_at, updated_at, deleted)
            SELECT DISTINCT ON (library_id)
                library_id, title, created_at, updated_at, deleted
            FROM knowledge_libraries
            ORDER BY library_id, updated_at DESC
            ON CONFLICT (library_id) DO NOTHING;

            CREATE TABLE IF NOT EXISTS knowledge_assets_new (
                library_id text NOT NULL REFERENCES knowledge_libraries_new (library_id),
                asset_id text NOT NULL,
                title text NOT NULL,
                modality text NOT NULL,
                asset_path text NOT NULL,
                current_index_version_id text,
                created_at timestamptz NOT NULL,
                PRIMARY KEY (library_id, asset_id)
            );
            INSERT INTO knowledge_assets_new (
                library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
            )
            SELECT DISTINCT ON (library_id, asset_id)
                library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
            FROM knowledge_assets
            ORDER BY library_id, asset_id, created_at DESC
            ON CONFLICT (library_id, asset_id) DO NOTHING;

            DROP TABLE IF EXISTS knowledge_assets;
            DROP TABLE IF EXISTS knowledge_libraries;
            ALTER TABLE knowledge_libraries_new RENAME TO knowledge_libraries;
            ALTER TABLE knowledge_assets_new RENAME TO knowledge_assets;
            """
        )


def _ensure_fk_hyperrag_asset_status() -> None:
    with _CONN.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            ("public.hyperrag_asset_status",),
        )
        if cur.fetchone()[0] is None:
            return
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
            WHERE conname = %s AND conrelid = %s::regclass
            """,
            (
                "hyperrag_asset_status_asset_fk",
                "public.hyperrag_asset_status",
            ),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            """
            ALTER TABLE hyperrag_asset_status
            ADD CONSTRAINT hyperrag_asset_status_asset_fk
            FOREIGN KEY (workspace, asset_id)
            REFERENCES knowledge_assets (library_id, asset_id)
            NOT VALID
            """
        )


def _ensure() -> None:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_libraries (
                library_id text PRIMARY KEY,
                title text NOT NULL,
                created_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                deleted boolean NOT NULL DEFAULT false
            );
            CREATE TABLE IF NOT EXISTS knowledge_assets (
                library_id text NOT NULL REFERENCES knowledge_libraries (library_id),
                asset_id text NOT NULL,
                title text NOT NULL,
                modality text NOT NULL,
                asset_path text NOT NULL,
                current_index_version_id text,
                created_at timestamptz NOT NULL,
                PRIMARY KEY (library_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_libraries_active
                ON knowledge_libraries (library_id)
                WHERE NOT deleted;
            CREATE TABLE IF NOT EXISTS knowledge_index_jobs (
                job_id text PRIMARY KEY,
                library_id text NOT NULL REFERENCES knowledge_libraries (library_id),
                asset_id text NOT NULL,
                status text NOT NULL,
                error_message text,
                created_at timestamptz NOT NULL,
                completed_at timestamptz,
                FOREIGN KEY (library_id, asset_id)
                    REFERENCES knowledge_assets (library_id, asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_index_jobs_asset_latest
                ON knowledge_index_jobs (library_id, asset_id, created_at DESC);
            """
        )
    _migrate_legacy_app_key()
    _ensure_fk_hyperrag_asset_status()


_ensure()


@dataclass
class LibraryRow:
    library_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    deleted: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "title": self.title,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
        }


@dataclass
class AssetRow:
    library_id: str
    asset_id: str
    title: str
    modality: str
    asset_path: str
    current_index_version_id: str | None
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "asset_id": self.asset_id,
            "title": self.title,
            "modality": self.modality,
            "asset_path": self.asset_path,
            "current_index_version_id": self.current_index_version_id,
            "created_at": stamp(self.created_at),
        }


def create_library(library_id: str, title: str) -> LibraryRow:
    ts = now()
    with _CONN.cursor() as cur:
        cur.execute(
            """
            SELECT deleted FROM knowledge_libraries
            WHERE library_id = %s
            """,
            (library_id,),
        )
        row = cur.fetchone()
        if row is not None and not row[0]:
            raise ValueError("library_exists")
        if row is not None and row[0]:
            cur.execute(
                """
                UPDATE knowledge_libraries
                SET title = %s, updated_at = %s, deleted = false
                WHERE library_id = %s
                """,
                (title, ts, library_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO knowledge_libraries (library_id, title, created_at, updated_at, deleted)
                VALUES (%s, %s, %s, %s, false)
                """,
                (library_id, title, ts, ts),
            )
    return LibraryRow(library_id, title, ts, ts, False)


def list_libraries(limit: int, after: str | None) -> list[LibraryRow]:
    params: list[Any] = []
    sql = """
        SELECT library_id, title, created_at, updated_at, deleted
        FROM knowledge_libraries
        WHERE NOT deleted
    """
    if after:
        sql += " AND library_id > %s"
        params.append(after)
    sql += " ORDER BY library_id ASC LIMIT %s"
    params.append(limit)
    with _CONN.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_library_from_row(row) for row in rows]


def get_library(library_id: str) -> LibraryRow | None:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            SELECT library_id, title, created_at, updated_at, deleted
            FROM knowledge_libraries
            WHERE library_id = %s AND NOT deleted
            """,
            (library_id,),
        )
        row = cur.fetchone()
    return None if row is None else _library_from_row(row)


def create_asset(
    library_id: str,
    asset_id: str,
    title: str,
    modality: str,
    asset_path: str,
) -> AssetRow:
    if get_library(library_id) is None:
        raise ValueError("library_not_found")
    ts = now()
    with _CONN.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM knowledge_assets
            WHERE library_id = %s AND asset_id = %s
            """,
            (library_id, asset_id),
        )
        if cur.fetchone():
            raise ValueError("asset_exists")
        cur.execute(
            """
            INSERT INTO knowledge_assets (
                library_id, asset_id, title, modality, asset_path,
                current_index_version_id, created_at
            ) VALUES (%s, %s, %s, %s, %s, NULL, %s)
            """,
            (library_id, asset_id, title, modality, asset_path, ts),
        )
    return AssetRow(library_id, asset_id, title, modality, asset_path, None, ts)


def list_assets(library_id: str, limit: int, after: str | None) -> list[AssetRow]:
    if get_library(library_id) is None:
        raise ValueError("library_not_found")
    params: list[Any] = [library_id]
    sql = """
        SELECT library_id, asset_id, title, modality, asset_path,
               current_index_version_id, created_at
        FROM knowledge_assets
        WHERE library_id = %s
    """
    if after:
        sql += " AND asset_id > %s"
        params.append(after)
    sql += " ORDER BY asset_id ASC LIMIT %s"
    params.append(limit)
    with _CONN.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_asset_from_row(row) for row in rows]


def get_asset(library_id: str, asset_id: str) -> AssetRow | None:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            SELECT library_id, asset_id, title, modality, asset_path,
                   current_index_version_id, created_at
            FROM knowledge_assets
            WHERE library_id = %s AND asset_id = %s
            """,
            (library_id, asset_id),
        )
        row = cur.fetchone()
    return None if row is None else _asset_from_row(row)


def set_current_index_version_id(library_id: str, asset_id: str, version_id: str) -> None:
    version = str(version_id or "").strip()
    if not version:
        raise ValueError("invalid_index_version")
    with _CONN.cursor() as cur:
        cur.execute(
            """
            UPDATE knowledge_assets
            SET current_index_version_id = %s
            WHERE library_id = %s AND asset_id = %s
            """,
            (version, library_id, asset_id),
        )
        if cur.rowcount == 0:
            raise ValueError("asset_not_found")


def clear_current_index_version_id(library_id: str, asset_id: str) -> None:
    with _CONN.cursor() as cur:
        cur.execute(
            """
            UPDATE knowledge_assets
            SET current_index_version_id = NULL
            WHERE library_id = %s AND asset_id = %s
            """,
            (library_id, asset_id),
        )


def get_latest_hyperrag_asset_status(library_id: str, asset_id: str) -> dict[str, Any] | None:
    with _CONN.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            ("public.hyperrag_asset_status",),
        )
        if cur.fetchone()[0] is None:
            return None
        cur.execute(
            """
            SELECT workspace, id, asset_id, status, error_message, updated_at
            FROM hyperrag_asset_status
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
            """
            INSERT INTO knowledge_index_jobs (
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
            """
            UPDATE knowledge_index_jobs
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
            """
            SELECT job_id, library_id, asset_id, status, error_message, created_at, completed_at
            FROM knowledge_index_jobs
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
            """
            SELECT job_id, library_id, asset_id, status, error_message, created_at, completed_at
            FROM knowledge_index_jobs
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
    if get_asset(library_id, asset_id) is None:
        return
    try:
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
    except Exception as exc:
        from psycopg.errors import ForeignKeyViolation

        if isinstance(exc, ForeignKeyViolation):
            return
        raise
