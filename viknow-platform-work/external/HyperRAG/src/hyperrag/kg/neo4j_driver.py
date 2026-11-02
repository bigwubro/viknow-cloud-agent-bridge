"""Neo4j async driver helpers: pool tuning and stale-connection retry."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from neo4j import AsyncDriver, AsyncGraphDatabase

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_MAX_CONNECTION_LIFETIME = 300.0
_DEFAULT_LIVENESS_CHECK_TIMEOUT = 5.0


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


def create_async_neo4j_driver(uri: str, auth: tuple[str, str]) -> AsyncDriver:
    """Build an async Neo4j driver with pool settings suited to long-lived services."""
    return AsyncGraphDatabase.driver(
        uri,
        auth=auth,
        max_connection_lifetime=_positive_float_env(
            "NEO4J_MAX_CONNECTION_LIFETIME",
            _DEFAULT_MAX_CONNECTION_LIFETIME,
        ),
        liveness_check_timeout=_positive_float_env(
            "NEO4J_LIVENESS_CHECK_TIMEOUT",
            _DEFAULT_LIVENESS_CHECK_TIMEOUT,
        ),
    )


async def run_with_neo4j_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    reset_driver: Callable[[], Awaitable[None]],
    operation_name: str,
    max_attempts: int = 2,
) -> T:
    """Retry once after resetting the driver when the pool hands out a stale connection."""
    from neo4j.exceptions import ServiceUnavailable

    last_error: ServiceUnavailable | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except ServiceUnavailable as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            logger.warning(
                'Neo4j %s failed with stale connection (attempt %s/%s): %s',
                operation_name,
                attempt,
                max_attempts,
                exc,
            )
            await reset_driver()
    assert last_error is not None
    raise last_error
