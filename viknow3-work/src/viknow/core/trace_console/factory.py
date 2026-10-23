"""Reuse the configured persistence identity without initializing stores."""

from viknow.core.config import (
    AppConfig,
    PersistenceBackend,
    resolve_session_postgres_dsn,
    resolve_session_sqlite_path,
)
from viknow.core.trace_console.reader import TraceReader


def build_trace_reader(config: AppConfig) -> TraceReader:
    options = config.trace_console
    return TraceReader(
        path=resolve_session_sqlite_path(state_dir=config.app.state_dir)
        if config.persistence.backend == PersistenceBackend.SQLITE
        else None,
        dsn=resolve_session_postgres_dsn(config.persistence)
        if config.persistence.backend == PersistenceBackend.POSTGRES
        else None,
        timeout=options.query_timeout_seconds,
        pool_size=options.pool_size,
    )
