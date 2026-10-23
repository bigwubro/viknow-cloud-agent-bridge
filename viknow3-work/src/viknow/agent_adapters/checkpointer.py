"""LangGraph checkpointer providers for DeepAgents."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from viknow.core.config import CheckpointerConfig, PersistenceBackend


class SqliteCheckpointerProvider:
    """Lazy async provider for a persistent LangGraph SQLite checkpointer."""

    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self._checkpointer: Any | None = None
        self._context: AbstractAsyncContextManager[Any] | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._context = AsyncSqliteSaver.from_conn_string(str(self.sqlite_path))
            self._checkpointer = await self._context.__aenter__()
            await self._checkpointer.setup()
            return self._checkpointer

    async def aclose(self) -> None:
        if self._context is None:
            return
        await self._context.__aexit__(None, None, None)
        self._context = None
        self._checkpointer = None


class PostgresCheckpointerProvider:
    """Lazy async provider for a persistent LangGraph Postgres checkpointer."""

    def __init__(self, postgres_dsn: str) -> None:
        self.postgres_dsn = postgres_dsn
        self._checkpointer: Any | None = None
        self._context: AbstractAsyncContextManager[Any] | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            self._context = AsyncPostgresSaver.from_conn_string(self.postgres_dsn)
            self._checkpointer = await self._context.__aenter__()
            await self._checkpointer.setup()
            return self._checkpointer

    async def aclose(self) -> None:
        if self._context is None:
            return
        await self._context.__aexit__(None, None, None)
        self._context = None
        self._checkpointer = None


def build_checkpointer_provider(config: CheckpointerConfig) -> Any | None:
    """Build the checkpointer provider for the resolved kernel checkpointer config."""

    if not config.enabled:
        return None
    if config.backend == PersistenceBackend.POSTGRES:
        if not config.postgres_dsn:
            raise RuntimeError("checkpointer.postgres_dsn is required for postgres backend")
        return PostgresCheckpointerProvider(config.postgres_dsn)
    sqlite_path = config.sqlite_path
    if sqlite_path is None:
        raise RuntimeError("checkpointer.sqlite_path is required for sqlite backend")
    return SqliteCheckpointerProvider(sqlite_path)
