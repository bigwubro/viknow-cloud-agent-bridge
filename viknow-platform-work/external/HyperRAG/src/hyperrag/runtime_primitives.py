from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import tiktoken


@dataclass
class BaseKVStorage:
    namespace: str
    workspace: str
    global_config: dict[str, Any]
    embedding_func: Any = None


@dataclass
class BaseVectorStorage:
    namespace: str
    workspace: str
    global_config: dict[str, Any]
    embedding_func: Any = None


EmbeddingFunc = Any
UnifiedLock = asyncio.Lock


@dataclass
class SimpleTokenizer:
    model_name: str
    fallback_model_name: str = "gpt-4o-mini"
    encoding_name: str = "cl100k_base"

    def __post_init__(self) -> None:
        self.used_fallback = False
        self.resolved_model_name = self.model_name
        try:
            self._encoding = tiktoken.encoding_for_model(self.model_name)
        except Exception:
            self.used_fallback = True
            self.resolved_model_name = self.fallback_model_name
            try:
                self._encoding = tiktoken.encoding_for_model(self.fallback_model_name)
            except Exception:
                self.resolved_model_name = self.encoding_name
                self._encoding = tiktoken.get_encoding(self.encoding_name)

    def encode(self, text: str) -> list[int]:
        return list(self._encoding.encode(str(text or "")))

    def decode(self, tokens: Iterable[int]) -> str:
        return self._encoding.decode(list(tokens))


@dataclass
class QueryParam:
    mode: str = "hybrid"
    chunk_top_k: int | None = None
    max_total_tokens: int | None = None


_data_init_lock = asyncio.Lock()
_namespace_locks: dict[tuple[str, str], asyncio.Lock] = {}


def initialize_share_data(workers: int = 1) -> None:
    _ = workers


def get_data_init_lock() -> asyncio.Lock:
    return _data_init_lock


def get_namespace_lock(namespace: str, workspace: str | None = None) -> asyncio.Lock:
    key = (str(namespace or ""), str(workspace or ""))
    lock = _namespace_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _namespace_locks[key] = lock
    return lock


async def try_initialize_namespace(namespace: str, workspace: str | None = None) -> None:
    get_namespace_lock(namespace, workspace)


def _reference_file_path(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    return str(chunk.get("file_path") or metadata.get("file_path") or "")


def generate_reference_list_from_chunks(
    chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    references: list[dict[str, str]] = []
    updated_chunks: list[dict[str, Any]] = []
    file_path_to_ref_id: dict[str, str] = {}
    for chunk in chunks or []:
        normalized_chunk = dict(chunk)
        file_path = _reference_file_path(normalized_chunk)
        if file_path and file_path != "unknown_source":
            ref_id = file_path_to_ref_id.get(file_path)
            if ref_id is None:
                ref_id = str(len(references) + 1)
                file_path_to_ref_id[file_path] = ref_id
                references.append({"reference_id": ref_id, "file_path": file_path})
            normalized_chunk["reference_id"] = ref_id
        updated_chunks.append(normalized_chunk)
    return references, updated_chunks


class PostgreSQLDB:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        workspace: str = "",
        connection: asyncpg.Connection | None = None,
    ) -> None:
        self.pool = pool
        self.workspace = workspace
        self.connection = connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PostgreSQLDB]:
        if self.connection is not None:
            async with self.connection.transaction():
                yield self
        else:
            async with self.pool.acquire() as conn, conn.transaction():
                yield PostgreSQLDB(self.pool, workspace=self.workspace, connection=conn)

    async def execute(
        self,
        sql: str,
        params: Any = None,
        *,
        upsert: bool = False,
        ignore_if_exists: bool = False,
    ) -> str:
        del upsert
        values = _normalize_sql_params(params)
        try:
            if self.connection is not None:
                return await self.connection.execute(sql, *values)
            async with self.pool.acquire() as conn:
                return await conn.execute(sql, *values)
        except Exception as exc:
            if ignore_if_exists and _is_ignorable_sql_error(exc):
                return "IGNORED"
            raise

    async def query(
        self,
        sql: str,
        params: Any = None,
        *,
        multirows: bool = False,
    ) -> Any:
        values = _normalize_sql_params(params)
        if self.connection is not None:
            if multirows:
                return [dict(row) for row in await self.connection.fetch(sql, *values)]
            row = await self.connection.fetchrow(sql, *values)
            return dict(row) if row is not None else None
        async with self.pool.acquire() as conn:
            if multirows:
                rows = await conn.fetch(sql, *values)
                return [dict(row) for row in rows]
            row = await conn.fetchrow(sql, *values)
            return dict(row) if row is not None else None

    async def close(self) -> None:
        return None


class ClientManager:
    _pool: asyncpg.Pool | None = None
    _pool_lock = asyncio.Lock()

    @classmethod
    async def get_client(cls) -> PostgreSQLDB:
        pool = await cls._ensure_pool()
        workspace = os.environ.get("POSTGRES_WORKSPACE", "")
        return PostgreSQLDB(pool, workspace=workspace)

    @classmethod
    async def release_client(cls, db: PostgreSQLDB) -> None:
        _ = db
        return None

    @classmethod
    async def close_pool(cls) -> None:
        async with cls._pool_lock:
            if cls._pool is not None:
                await cls._pool.close()
                cls._pool = None

    @classmethod
    async def _ensure_pool(cls) -> asyncpg.Pool:
        if cls._pool is not None:
            return cls._pool
        async with cls._pool_lock:
            if cls._pool is None:
                cls._pool = await asyncpg.create_pool(
                    user=_get_required_env("POSTGRES_USER"),
                    password=_get_required_env("POSTGRES_PASSWORD"),
                    database=os.environ.get("POSTGRES_DATABASE")
                    or _get_required_env("POSTGRES_DB", fallback_key="POSTGRES_DATABASE"),
                    host=os.environ.get("POSTGRES_HOST", "localhost"),
                    port=int(os.environ.get("POSTGRES_PORT", "5432")),
                    min_size=1,
                    max_size=int(os.environ.get("POSTGRES_MAX_CONNECTIONS", "20")),
                    command_timeout=60,
                )
            return cls._pool


def _normalize_sql_params(params: Any) -> list[Any]:
    if params is None:
        return []
    if isinstance(params, dict):
        dollar_keys = [
            key for key in params if isinstance(key, str) and re.fullmatch(r"\$\d+", key)
        ]
        if dollar_keys:
            ordered = sorted(dollar_keys, key=lambda key: int(key[1:]))
            return [params[key] for key in ordered]
        return list(params.values())
    if isinstance(params, tuple):
        return list(params)
    if isinstance(params, list):
        return params
    if isinstance(params, set):
        return list(params)
    return [params]


def _is_ignorable_sql_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "already exists" in message
        or "duplicate" in message
        or "exists" in message
        and "index" in message
    )


def _get_required_env(primary_key: str, *, fallback_key: str | None = None) -> str:
    value = os.environ.get(primary_key)
    if value:
        return value
    if fallback_key:
        fallback_value = os.environ.get(fallback_key)
        if fallback_value:
            return fallback_value
    raise ValueError(f"Missing required environment variable: {primary_key}")
