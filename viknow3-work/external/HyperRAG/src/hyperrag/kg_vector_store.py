from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .runtime_primitives import ClientManager, SimpleTokenizer, get_data_init_lock

logger = logging.getLogger(__name__)


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "")).strip("_").lower()
    if not normalized:
        raise ValueError("identifier cannot be empty")
    return normalized


def _qualified_table(schema: str, table: str) -> str:
    return f"{_safe_identifier(schema)}.{_safe_identifier(table)}"


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"unsupported JSON type: {type(value)!r}")


@dataclass
class KGEmbeddingClient:
    model: str
    base_url: str
    api_key: str | None
    provider: str = "vllm"
    timeout: int = 120
    batch_size: int = 16
    max_concurrency: int = 32
    max_input_tokens: int = 12000
    _client: AsyncOpenAI | None = field(default=None, init=False, repr=False)
    _tokenizer: SimpleTokenizer | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")

    def _client_or_create(self) -> AsyncOpenAI:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": self.timeout,
            }
            if self.provider != "openai":
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def _tokenizer_or_create(self) -> SimpleTokenizer:
        if self._tokenizer is None:
            self._tokenizer = SimpleTokenizer(model_name=self.model)
        return self._tokenizer

    def _truncate_text(self, text: str) -> str:
        normalized = str(text or "")
        max_input_tokens = max(1, int(self.max_input_tokens or 12000))
        tokenizer = self._tokenizer_or_create()
        encoded = tokenizer.encode(normalized)
        if len(encoded) <= max_input_tokens:
            return normalized
        logger.warning(
            "KGEmbeddingClient: truncating embedding input for model=%s from %s to %s tokens",
            self.model,
            len(encoded),
            max_input_tokens,
        )
        return tokenizer.decode(encoded[:max_input_tokens])

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = max(1, int(self.batch_size or 16))
        client = self._client_or_create()
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def encode_batch(start: int) -> list[list[float]]:
            async with semaphore:
                batch = [self._truncate_text(text) for text in texts[start : start + batch_size]]
                response = await client.embeddings.create(model=self.model, input=batch)
            if len(response.data) != len(batch):
                raise ValueError("Embedding response count does not match input batch size")
            try:
                from hyperrag.observability.index_usage import (
                    record_generation,
                    usage_from_openai_response,
                )

                record_generation(
                    name="kg_embedding",
                    model=self.model,
                    usage=usage_from_openai_response(response),
                )
            except Exception:
                pass
            return [list(item.embedding) for item in response.data]

        results = await asyncio.gather(
            *(encode_batch(start) for start in range(0, len(texts), batch_size)),
            return_exceptions=True,
        )
        out: list[list[float]] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            out.extend(result)
        return out


def _pgvector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


@dataclass
class KGVectorStore:
    workspace: str
    schema: str
    vector_dim: int
    chunk_table: str
    entity_table: str
    relation_table: str
    db: Any | None = field(default=None)
    _initialized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.schema = _safe_identifier(self.schema)
        self.chunk_table = _safe_identifier(self.chunk_table)
        self.entity_table = _safe_identifier(self.entity_table)
        self.relation_table = _safe_identifier(self.relation_table)

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with get_data_init_lock():
            if self.db is None:
                if ClientManager is None:
                    raise RuntimeError(
                        "PostgreSQL vector backend requires HyperRAG database configuration"
                    )
                self.db = await ClientManager.get_client()
            try:
                await self.db.execute(
                    "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public",
                    upsert=True,
                )
            except Exception as exc:
                logger.warning("failed to create pgvector extension: %s", exc)
            await self.db.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}", upsert=True)
            for table_name in (self.chunk_table, self.entity_table, self.relation_table):
                await self.db.execute(self._create_table_sql(table_name), upsert=True)
                for index_sql in self._create_index_sql(table_name):
                    try:
                        await self.db.execute(index_sql, upsert=True, ignore_if_exists=True)
                    except Exception as exc:
                        if "hnsw" in index_sql.lower():
                            logger.warning(
                                "failed to create hnsw index for %s: %s", table_name, exc
                            )
                        else:
                            raise
        self._initialized = True

    async def finalize(self) -> None:
        if self.db is not None and ClientManager is not None:
            await ClientManager.release_client(self.db)
        self.db = None
        self._initialized = False

    async def ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def upsert_chunks(self, rows: list[dict[str, Any]]) -> None:
        await self._upsert_rows(self.chunk_table, rows)

    async def upsert_entities(self, rows: list[dict[str, Any]]) -> None:
        await self._upsert_rows(self.entity_table, rows)

    async def upsert_relations(self, rows: list[dict[str, Any]]) -> None:
        await self._upsert_rows(self.relation_table, rows)

    async def query_chunks(
        self,
        *,
        query_vector: list[float],
        workspace: str | None = None,
        top_k: int = 10,
        min_similarity: float = -1.0,
    ) -> list[dict[str, Any]]:
        return await self._query_rows(
            self.chunk_table,
            query_vector=query_vector,
            workspace=workspace,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    async def query_entities(
        self,
        *,
        query_vector: list[float],
        workspace: str | None = None,
        top_k: int = 10,
        min_similarity: float = -1.0,
    ) -> list[dict[str, Any]]:
        return await self._query_rows(
            self.entity_table,
            query_vector=query_vector,
            workspace=workspace,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    async def query_relations(
        self,
        *,
        query_vector: list[float],
        workspace: str | None = None,
        top_k: int = 10,
        min_similarity: float = -1.0,
    ) -> list[dict[str, Any]]:
        return await self._query_rows(
            self.relation_table,
            query_vector=query_vector,
            workspace=workspace,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    async def get_chunks_by_ids(
        self, *, workspace: str | None, chunk_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        chunk_id_list = [str(chunk_id) for chunk_id in chunk_ids if str(chunk_id).strip()]
        if not chunk_id_list:
            return []
        sql = f"""
            SELECT workspace, item_id, file_path, content, metadata
            FROM {_qualified_table(self.schema, self.chunk_table)}
            WHERE workspace = $1 AND item_id = ANY($2)
        """
        rows = await self.db.query(
            sql, [workspace or self.workspace, chunk_id_list], multirows=True
        )
        return [self._decode_row(row) for row in rows or []]

    async def query_cross_workspace(
        self,
        *,
        kind: str,
        query_vector: list[float],
        workspaces: Collection[str],
        top_k: int = 10,
        min_similarity: float = -1.0,
    ) -> list[dict[str, Any]]:
        tables = {
            "chunks": self.chunk_table,
            "entities": self.entity_table,
            "relations": self.relation_table,
        }
        if kind not in tables:
            raise ValueError(f"unknown vector kind: {kind}")
        if not workspaces:
            return []
        return await self._query_rows(
            tables[kind],
            query_vector=query_vector,
            workspace=None,
            workspaces=workspaces,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    async def get_chunks_by_keys(
        self, *, chunk_keys: Collection[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        """Fetch exact source identities, never the Cartesian product of IDs and libraries."""
        if not chunk_keys:
            return []
        await self.ensure_initialized()
        keys = list(dict.fromkeys(chunk_keys))
        sql = f"""
            SELECT workspace, item_id, file_path, content, metadata
            FROM {_qualified_table(self.schema, self.chunk_table)}
            WHERE (workspace, item_id) IN (
                SELECT * FROM unnest($1::text[], $2::text[])
            )
        """
        rows = await self.db.query(
            sql, [[key[0] for key in keys], [key[1] for key in keys]], multirows=True
        )
        return [self._decode_row(row) for row in rows or []]

    async def get_chunks_by_file_path(
        self, *, workspace: str | None, file_path: str
    ) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        normalized_file_path = str(file_path or "").strip()
        if not normalized_file_path:
            return []
        sql = f"""
            SELECT workspace, item_id, file_path, content, metadata
            FROM {_qualified_table(self.schema, self.chunk_table)}
            WHERE workspace = $1 AND file_path = $2
        """
        rows = await self.db.query(
            sql,
            [workspace or self.workspace, normalized_file_path],
            multirows=True,
        )
        return [self._decode_row(row) for row in rows or []]

    async def find_entity_synonyms(
        self,
        *,
        workspace: str | None,
        normalized_keys: list[str],
        exclude_ids: Iterable[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        keys = [str(key) for key in normalized_keys if str(key).strip()]
        if not keys:
            return []
        excluded = [str(item_id) for item_id in (exclude_ids or []) if str(item_id).strip()]
        sql = f"""
            SELECT workspace, item_id, file_path, content, metadata
            FROM {_qualified_table(self.schema, self.entity_table)}
            WHERE workspace = $1
              AND (
                    metadata->>'normalized_name' = ANY($2)
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(COALESCE(metadata->'alias_keys', '[]'::jsonb)) AS alias(value)
                        WHERE alias.value = ANY($2)
                    )
                  )
              AND NOT (item_id = ANY($3))
            LIMIT $4
        """
        rows = await self.db.query(
            sql, [workspace or self.workspace, keys, excluded, max(1, int(limit))], multirows=True
        )
        return [self._decode_row(row) for row in rows or []]

    async def delete_by_file_path(self, *, workspace: str | None, file_path: str) -> None:
        await self.ensure_initialized()
        target_workspace = workspace or self.workspace
        for table_name in (self.chunk_table, self.entity_table, self.relation_table):
            sql = f"DELETE FROM {_qualified_table(self.schema, table_name)} WHERE workspace = $1 AND file_path = $2"
            await self.db.execute(
                sql, {"workspace": target_workspace, "file_path": file_path}, upsert=True
            )

    async def delete_chunks_by_file_path(self, *, workspace: str | None, file_path: str) -> None:
        await self.ensure_initialized()
        sql = f"DELETE FROM {_qualified_table(self.schema, self.chunk_table)} WHERE workspace = $1 AND file_path = $2"
        await self.db.execute(
            sql,
            {"workspace": workspace or self.workspace, "file_path": file_path},
            upsert=True,
        )

    async def delete_graph_vectors_by_asset_id(
        self, *, workspace: str | None, asset_id: str
    ) -> None:
        await self.ensure_initialized()
        for table_name in (self.entity_table, self.relation_table):
            sql = f"""
                DELETE FROM {_qualified_table(self.schema, table_name)}
                WHERE workspace = $1 AND metadata->>'doc_id' = $2
            """
            await self.db.execute(
                sql,
                [workspace or self.workspace, str(asset_id)],
                upsert=True,
            )

    async def delete_chunk(self, *, workspace: str | None, chunk_id: str) -> None:
        await self.ensure_initialized()
        sql = f"DELETE FROM {_qualified_table(self.schema, self.chunk_table)} WHERE workspace = $1 AND item_id = $2"
        await self.db.execute(
            sql,
            [workspace or self.workspace, str(chunk_id)],
            upsert=True,
        )

    async def clear_workspace(self, *, workspace: str | None = None) -> None:
        await self.ensure_initialized()
        target_workspace = workspace or self.workspace
        for table_name in (self.chunk_table, self.entity_table, self.relation_table):
            sql = f"DELETE FROM {_qualified_table(self.schema, table_name)} WHERE workspace = $1"
            await self.db.execute(sql, {"workspace": target_workspace}, upsert=True)

    async def clear_workspaces_by_prefix(self, workspace_prefix: str) -> None:
        await self.ensure_initialized()
        like_pattern = f"{workspace_prefix}%"
        for table_name in (self.chunk_table, self.entity_table, self.relation_table):
            sql = f"DELETE FROM {_qualified_table(self.schema, table_name)} WHERE workspace LIKE $1"
            await self.db.execute(sql, {"workspace_prefix": like_pattern}, upsert=True)

    async def _upsert_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        await self.ensure_initialized()
        if not rows:
            return
        sql = self._upsert_sql(table_name)
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            payload = {
                "workspace": row["workspace"],
                "item_id": row["item_id"],
                "file_path": row["file_path"],
                "content": row.get("content") or "",
                "embedding": _pgvector_literal(list(row["embedding"])),
                "metadata": json.dumps(metadata, ensure_ascii=False, default=_json_default),
            }
            await self.db.execute(sql, payload, upsert=True)

    async def _query_rows(
        self,
        table_name: str,
        *,
        query_vector: list[float],
        workspace: str | None,
        top_k: int,
        min_similarity: float,
        workspaces: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        sql = f"""
            SELECT workspace, item_id, file_path, content, metadata,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM {_qualified_table(self.schema, table_name)}
            WHERE {"workspace = ANY($2)" if workspaces is not None else "workspace = $2"}
              AND 1 - (embedding <=> $1::vector) >= $3
            ORDER BY embedding <=> $1::vector
            LIMIT $4
        """
        rows = await self.db.query(
            sql,
            [
                _pgvector_literal(list(query_vector)),
                list(workspaces) if workspaces is not None else workspace or self.workspace,
                float(min_similarity),
                max(1, int(top_k)),
            ],
            multirows=True,
        )
        return [self._decode_row(row) for row in rows or []]

    def _create_table_sql(self, table_name: str) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {_qualified_table(self.schema, table_name)} (
                workspace TEXT NOT NULL,
                item_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding VECTOR({int(self.vector_dim)}),
                metadata JSONB DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (workspace, item_id)
            )
        """

    def _create_index_sql(self, table_name: str) -> list[str]:
        qualified = _qualified_table(self.schema, table_name)
        table_key = _safe_identifier(table_name)
        return [
            f"CREATE INDEX IF NOT EXISTS idx_{table_key}_workspace_file ON {qualified} (workspace, file_path)",
            f"CREATE INDEX IF NOT EXISTS idx_{table_key}_metadata_gin ON {qualified} USING GIN (metadata)",
            f"CREATE INDEX IF NOT EXISTS idx_{table_key}_embedding_hnsw ON {qualified} USING hnsw (embedding vector_cosine_ops)",
        ]

    def _upsert_sql(self, table_name: str) -> str:
        qualified = _qualified_table(self.schema, table_name)
        return f"""
            INSERT INTO {qualified} (workspace, item_id, file_path, content, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (workspace, item_id)
            DO UPDATE SET
                file_path = EXCLUDED.file_path,
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
        """

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row, dict) else {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        result = dict(row)
        result["metadata"] = metadata if isinstance(metadata, dict) else {}
        return result
