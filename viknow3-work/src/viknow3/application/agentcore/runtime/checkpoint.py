"""LangGraph Postgres checkpointer in the agent database. thread_id is the session id."""

from __future__ import annotations

import asyncio
import atexit
from typing import Any

from viknow3.application.agentcore.runtime.loop import LOOP
from viknow3.pg import AGENT, dsn, require_test_database

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations")

_SAVER: Any = None
_POOL: Any = None
_GUARD: asyncio.Lock | None = None


async def checkpointer() -> Any:
    """The shared AsyncPostgresSaver. Must be awaited on the agent loop."""
    global _SAVER, _POOL, _GUARD
    if _SAVER is not None:
        return _SAVER
    if _GUARD is None:
        _GUARD = asyncio.Lock()
    async with _GUARD:
        if _SAVER is not None:
            return _SAVER
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            dsn(AGENT),
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        _POOL, _SAVER = pool, saver
        atexit.register(_close_pool)
        return saver


def _close_pool() -> None:
    if _POOL is not None and not LOOP.in_loop():
        try:
            LOOP.call(_POOL.close(), timeout=5)
        except Exception:
            pass


def reset_agent_tables() -> None:
    """Clear checkpoints in the test database."""
    require_test_database(AGENT)

    async def clear() -> None:
        await checkpointer()
        async with _POOL.connection() as conn:
            for table in CHECKPOINT_TABLES[:-1]:
                await conn.execute(f"TRUNCATE {table}")

    LOOP.call(clear())
