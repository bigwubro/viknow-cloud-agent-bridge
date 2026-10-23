"""Factory for Feishu synchronization persistence."""

from __future__ import annotations

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_session_postgres_dsn,
    resolve_session_sqlite_path,
)
from viknow.core.knowledge.feishu.postgres_store import PostgresFeishuSyncStore
from viknow.core.knowledge.feishu.sqlite_store import SqliteFeishuSyncStore
from viknow.core.knowledge.feishu.store import FeishuSyncStore


def build_feishu_sync_store(config: AppConfig) -> FeishuSyncStore:
    if config.persistence.backend == PersistenceBackend.SQLITE:
        return SqliteFeishuSyncStore(resolve_session_sqlite_path(state_dir=config.app.state_dir))
    if config.persistence.backend == PersistenceBackend.POSTGRES:
        return PostgresFeishuSyncStore(resolve_session_postgres_dsn(config.persistence))
    raise ValueError(f"unsupported persistence backend: {config.persistence.backend}")


__all__ = ["build_feishu_sync_store"]
