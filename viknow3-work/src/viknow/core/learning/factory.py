"""Factory for the optional Learning Projection persistence backend."""

from __future__ import annotations

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_learning_postgres_dsn,
)
from viknow.core.learning.postgres_store import PostgresLearningProjectionStore
from viknow.core.learning.sqlite_store import SqliteLearningProjectionStore
from viknow.core.learning.store import LearningProjectionStore
from viknow.core.postgres_bootstrap import ensure_learning_postgres_schema


def build_learning_projection_store(config: AppConfig) -> LearningProjectionStore:
    """Build storage only when Learning Projection is explicitly enabled."""

    if not config.learning.enabled:
        raise RuntimeError("learning projection is disabled")
    if config.persistence.backend == PersistenceBackend.SQLITE:
        return SqliteLearningProjectionStore(config.app.state_dir / "learning.sqlite")
    if config.persistence.backend == PersistenceBackend.POSTGRES:
        ensure_learning_postgres_schema(config.persistence)
        return PostgresLearningProjectionStore(resolve_learning_postgres_dsn(config.persistence))
    raise ValueError(f"unsupported learning persistence backend: {config.persistence.backend}")
