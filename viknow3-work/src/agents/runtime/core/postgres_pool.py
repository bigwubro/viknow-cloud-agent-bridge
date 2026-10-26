"""Process-wide Postgres connection pools for ViKnow sync stores.

Session / upload / scheduler-metrics stores previously called ``psycopg.connect``
on every query. Cross-host Dev Container → remote Postgres pays ~300–500ms per
cold connect, which dominates list_sessions / timeline latency.

This module keeps one :class:`psycopg_pool.ConnectionPool` per DSN (search_path
is part of the DSN, so session vs scheduler schemas naturally get separate pools).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Dev Container 连远程 PG 时预热 1～2 条连接；上限留足并发（session/upload/metrics 共用）。
_DEFAULT_MIN_SIZE = 1
_DEFAULT_MAX_SIZE = 100
_DEFAULT_TIMEOUT = 10.0

_lock = threading.Lock()
_pools: dict[str, ConnectionPool] = {}


def get_pool(
    dsn: str,
    *,
    min_size: int = _DEFAULT_MIN_SIZE,
    max_size: int = _DEFAULT_MAX_SIZE,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ConnectionPool:
    """Return the shared pool for ``dsn``, creating it on first use."""
    with _lock:
        pool = _pools.get(dsn)
        if pool is not None:
            return pool
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        _pools[dsn] = pool
        return pool


@contextmanager
def connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """Borrow a pooled connection for ``dsn``.

    Caller is responsible for ``commit`` / ``rollback``. Returning from the
    ``with`` block puts the connection back into the pool (not closed).
    """
    pool = get_pool(dsn)
    with pool.connection() as conn:
        yield conn


def close_all_pools() -> None:
    """Close every registered pool (FastAPI lifespan shutdown)."""
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


__all__ = [
    "close_all_pools",
    "connection",
    "get_pool",
]
