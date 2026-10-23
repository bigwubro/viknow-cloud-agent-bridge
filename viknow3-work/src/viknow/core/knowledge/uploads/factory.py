"""Factory for knowledge upload persistence."""

from __future__ import annotations

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_session_postgres_dsn,
    resolve_session_sqlite_path,
)
from viknow.core.knowledge.uploads.postgres_store import PostgresKnowledgeUploadStore
from viknow.core.knowledge.uploads.sqlite_store import SqliteKnowledgeUploadStore
from viknow.core.knowledge.uploads.store import KnowledgeUploadStore


def build_knowledge_upload_store(config: AppConfig) -> KnowledgeUploadStore:
    if config.persistence.backend == PersistenceBackend.SQLITE:
        path = resolve_session_sqlite_path(state_dir=config.app.state_dir)
        return SqliteKnowledgeUploadStore(path)
    if config.persistence.backend == PersistenceBackend.POSTGRES:
        return PostgresKnowledgeUploadStore(
            resolve_session_postgres_dsn(config.persistence),
            statement_timeout_seconds=config.persistence.postgres.upload_statement_timeout_seconds,
        )
    raise ValueError(f"unsupported persistence backend: {config.persistence.backend}")
