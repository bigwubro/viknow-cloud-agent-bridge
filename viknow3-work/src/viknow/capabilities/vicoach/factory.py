"""Factory for intelligent video training persistence."""

from __future__ import annotations

from viknow.capabilities.vicoach.postgres_store import PostgresVicoachStore
from viknow.capabilities.vicoach.store import VicoachStore
from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_session_postgres_dsn,
)
from viknow.core.postgres_bootstrap import ensure_viknow_postgres_schemas


def build_vicoach_store(config: AppConfig) -> VicoachStore:
    """Build ViCoach's standalone tables in the configured Postgres schema."""

    if config.persistence.backend != PersistenceBackend.POSTGRES:
        raise RuntimeError("vicoach requires PostgreSQL persistence")
    ensure_viknow_postgres_schemas(config.persistence)
    return PostgresVicoachStore(resolve_session_postgres_dsn(config.persistence))
