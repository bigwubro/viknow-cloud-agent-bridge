"""Factory for ViKnow session persistence backends."""

from __future__ import annotations

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_session_postgres_dsn,
    resolve_session_sqlite_path,
)
from viknow.core.postgres_bootstrap import ensure_viknow_postgres_schemas
from viknow.core.session.postgres_store import PostgresSessionStore
from viknow.core.session.sqlite_store import SqliteSessionStore
from viknow.core.session.store import SessionStore


def build_session_store(config: AppConfig) -> SessionStore:
    """Build the configured session store implementation."""

    if config.persistence.backend == PersistenceBackend.SQLITE:
        path = resolve_session_sqlite_path(state_dir=config.app.state_dir)
        return SqliteSessionStore(path)
    if config.persistence.backend == PersistenceBackend.POSTGRES:
        ensure_viknow_postgres_schemas(config.persistence)
        return PostgresSessionStore(resolve_session_postgres_dsn(config.persistence))
    raise ValueError(f"unsupported persistence backend: {config.persistence.backend}")
