"""Postgres persistence for knowledge upload records."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from viknow.core.knowledge.uploads.domain import (
    KnowledgeUploadFailureKind,
    KnowledgeUploadRecord,
    KnowledgeUploadSource,
    KnowledgeUploadStatus,
)
from viknow.core.postgres_pool import connection as pooled_connection
from viknow.core.session.mapping import dump_dt, load_dt, now


class PostgresKnowledgeUploadStore:
    """Postgres-backed knowledge upload registry (session schema)."""

    def __init__(self, dsn: str, *, statement_timeout_seconds: float = 60) -> None:
        self.dsn = dsn
        self.statement_timeout_seconds = statement_timeout_seconds
        self._initialize()

    def add(self, record: KnowledgeUploadRecord) -> KnowledgeUploadRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_uploads (
                    id, library_id, asset_id, display_name, original_filename,
                    object_key, size_bytes, content_type, status, local_path,
                    job_id, failure_kind, error_message, source_json,
                    created_at, updated_at, deleted_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                self._row_values(record),
            )
        return record

    def get(self, upload_id: str, *, include_deleted: bool = False) -> KnowledgeUploadRecord | None:
        query = "SELECT * FROM knowledge_uploads WHERE id = %s"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._connect() as conn:
            row = conn.execute(query, (upload_id,)).fetchone()
        return self._from_row(row) if row is not None else None

    def list_for_library(
        self,
        *,
        library_id: str,
        include_deleted: bool = False,
    ) -> list[KnowledgeUploadRecord]:
        query = "SELECT * FROM knowledge_uploads WHERE library_id = %s"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, (library_id,)).fetchall()
        return [self._from_row(row) for row in rows]

    def update(self, record: KnowledgeUploadRecord) -> KnowledgeUploadRecord:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE knowledge_uploads
                SET library_id = %s, asset_id = %s, display_name = %s, original_filename = %s,
                    object_key = %s, size_bytes = %s, content_type = %s, status = %s,
                    local_path = %s, job_id = %s, failure_kind = %s, error_message = %s,
                    source_json = %s, updated_at = %s, deleted_at = %s
                WHERE id = %s
                """,
                (
                    record.library_id,
                    record.asset_id,
                    record.display_name,
                    record.original_filename,
                    record.object_key,
                    record.size_bytes,
                    record.content_type,
                    record.status.value,
                    record.local_path,
                    record.job_id,
                    record.failure_kind.value if record.failure_kind is not None else None,
                    record.error_message,
                    record.source.model_dump_json() if record.source is not None else None,
                    dump_dt(record.updated_at),
                    dump_dt(record.deleted_at) if record.deleted_at else None,
                    record.id,
                ),
            )
        return record

    def soft_delete(self, upload_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_uploads
                SET deleted_at = %s, updated_at = %s
                WHERE id = %s AND deleted_at IS NULL
                """,
                (dump_dt(now()), dump_dt(now()), upload_id),
            )
            return cursor.rowcount > 0

    def list_display_names(self, *, library_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT display_name FROM knowledge_uploads
                WHERE library_id = %s AND deleted_at IS NULL
                """,
                (library_id,),
            ).fetchall()
        return {str(row["display_name"]) for row in rows}

    def _initialize(self) -> None:
        with self._connect() as conn:
            relation = conn.execute(
                "SELECT to_regclass('knowledge_uploads') AS relation"
            ).fetchone()
            if relation["relation"] is None:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_uploads (
                        id TEXT PRIMARY KEY,
                        library_id TEXT NOT NULL,
                        asset_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        original_filename TEXT NOT NULL,
                        object_key TEXT NOT NULL,
                        size_bytes BIGINT NOT NULL,
                        content_type TEXT,
                        status TEXT NOT NULL,
                        local_path TEXT,
                        job_id TEXT,
                        failure_kind TEXT,
                        error_message TEXT,
                        source_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        deleted_at TEXT
                    )
                    """
                )
            columns = {
                row["attname"]
                for row in conn.execute(
                    "SELECT attname FROM pg_attribute "
                    "WHERE attrelid = to_regclass('knowledge_uploads') "
                    "AND attnum > 0 AND NOT attisdropped"
                ).fetchall()
            }
            # ALTER IF NOT EXISTS still takes an exclusive relation lock. Only
            # migrate truly missing columns; an ordinary reader must not block startup.
            for column in ("failure_kind", "source_json"):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE knowledge_uploads ADD COLUMN IF NOT EXISTS {column} TEXT"
                    )
            index = conn.execute(
                "SELECT to_regclass('idx_knowledge_uploads_library_created') AS relation"
            ).fetchone()
            if index["relation"] is None:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_knowledge_uploads_library_created "
                    "ON knowledge_uploads(library_id, created_at DESC)"
                )

    @contextmanager
    def _connect(self):
        with pooled_connection(self.dsn) as conn:
            try:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self.statement_timeout_seconds * 1000))),),
                )
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _row_values(record: KnowledgeUploadRecord) -> tuple:
        return (
            record.id,
            record.library_id,
            record.asset_id,
            record.display_name,
            record.original_filename,
            record.object_key,
            record.size_bytes,
            record.content_type,
            record.status.value,
            record.local_path,
            record.job_id,
            record.failure_kind.value if record.failure_kind is not None else None,
            record.error_message,
            record.source.model_dump_json() if record.source is not None else None,
            dump_dt(record.created_at),
            dump_dt(record.updated_at),
            dump_dt(record.deleted_at) if record.deleted_at else None,
        )

    @staticmethod
    def _from_row(row: dict[str, Any]) -> KnowledgeUploadRecord:
        raw_kind = row.get("failure_kind")
        return KnowledgeUploadRecord(
            id=row["id"],
            library_id=row["library_id"],
            asset_id=row["asset_id"],
            display_name=row["display_name"],
            original_filename=row["original_filename"],
            object_key=row["object_key"],
            size_bytes=int(row["size_bytes"]),
            content_type=row["content_type"],
            status=KnowledgeUploadStatus(row["status"]),
            local_path=row["local_path"],
            job_id=row["job_id"],
            failure_kind=KnowledgeUploadFailureKind(raw_kind) if raw_kind else None,
            error_message=row["error_message"],
            source=(
                KnowledgeUploadSource.model_validate_json(row["source_json"])
                if row.get("source_json")
                else None
            ),
            created_at=load_dt(row["created_at"]),
            updated_at=load_dt(row["updated_at"]),
            deleted_at=load_dt(row["deleted_at"]),
        )
