"""SQLite persistence for knowledge upload records."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from viknow.core.knowledge.uploads.domain import (
    KnowledgeUploadFailureKind,
    KnowledgeUploadRecord,
    KnowledgeUploadSource,
    KnowledgeUploadStatus,
)
from viknow.core.session.mapping import dump_dt, load_dt, now


class SqliteKnowledgeUploadStore:
    """SQLite-backed knowledge upload registry (shares session DB file)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(record),
            )
        return record

    def get(self, upload_id: str, *, include_deleted: bool = False) -> KnowledgeUploadRecord | None:
        query = "SELECT * FROM knowledge_uploads WHERE id = ?"
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
        query = "SELECT * FROM knowledge_uploads WHERE library_id = ?"
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
                SET library_id = ?, asset_id = ?, display_name = ?, original_filename = ?,
                    object_key = ?, size_bytes = ?, content_type = ?, status = ?,
                    local_path = ?, job_id = ?, failure_kind = ?, error_message = ?,
                    source_json = ?, updated_at = ?, deleted_at = ?
                WHERE id = ?
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
                SET deleted_at = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (dump_dt(now()), dump_dt(now()), upload_id),
            )
            return cursor.rowcount > 0

    def list_display_names(self, *, library_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT display_name FROM knowledge_uploads
                WHERE library_id = ? AND deleted_at IS NULL
                """,
                (library_id,),
            ).fetchall()
        return {str(row["display_name"]) for row in rows}

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_uploads (
                    id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
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
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_uploads_library_created
                    ON knowledge_uploads(library_id, created_at DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_uploads)")}
            if "failure_kind" not in columns:
                conn.execute("ALTER TABLE knowledge_uploads ADD COLUMN failure_kind TEXT")
            if "source_json" not in columns:
                conn.execute("ALTER TABLE knowledge_uploads ADD COLUMN source_json TEXT")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

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
    def _from_row(row: sqlite3.Row) -> KnowledgeUploadRecord:
        raw_kind = row["failure_kind"]
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
                if row["source_json"]
                else None
            ),
            created_at=load_dt(row["created_at"]),
            updated_at=load_dt(row["updated_at"]),
            deleted_at=load_dt(row["deleted_at"]),
        )
