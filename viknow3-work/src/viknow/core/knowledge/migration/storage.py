"""Direct PostgreSQL / Neo4j access for knowledge migration.

Migration reads and writes the HyperRAG-owned stores directly instead of going
through HyperRAG's runtime loop, so it needs the storage connection parameters
from the materialized HyperRAG config (the same env block the adapter injects
into the process). The table names that depend on embedding configuration are
resolved here, mirroring HyperRAG's own naming rules.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

import psycopg
import yaml

from viknow.core.config import AppConfig, materialize_hyperrag_config

# ---------------------------------------------------------------------------
# config resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationStorageConfig:
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_database: str
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str | None = None
    kg_schema: str = "hyperrag_kg"
    kg_chunk_table: str = "kg_chunk_vectors"
    kg_entity_table: str = "kg_entity_vectors"
    kg_relation_table: str = "kg_relation_vectors"
    embedding_dim: int = 1024
    video_embedding_model: str | None = None
    video_embedding_dim: int = 1024
    video_segment_table_base: str = "hyperrag_video_segments"


def _env_value(env: dict[str, Any], key: str, default: str | None = None) -> str | None:
    return str(env.get(key) or "").strip() or default


def build_migration_storage_config(config: AppConfig) -> MigrationStorageConfig:
    """Resolve storage connection params from the materialized HyperRAG config.

    Values are taken from the rendered config's ``env`` block (postgres/neo4j),
    with the process environment taking precedence for overridden values.
    Missing connection params fail fast instead of silently defaulting to a
    localhost host that may be the wrong database.
    """
    config_path = materialize_hyperrag_config(config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    yaml_top = {str(k): v for k, v in raw.items() if k != "env"}
    yaml_env = {str(k): v for k, v in (raw.get("env") or {}).items()}

    def pick(key: str) -> str | None:
        return _env_value(os.environ, key) or _env_value(yaml_env, key)

    # Embedding/video fields live in the rendered config's top level (HyperRAG
    # flattens models.* into top-level keys), while connection params live in
    # the env block; merge both so either location is honored.
    raw = {**yaml_top, **yaml_env}

    missing = [
        name
        for name in (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DATABASE",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
        )
        if not pick(name)
    ]
    if missing:
        raise RuntimeError(f"migration storage config missing: {', '.join(missing)}")

    kg_schema = str(raw.get("kg_vector_schema") or "hyperrag_kg")
    chunk_table = str(raw.get("kg_vector_chunk_table") or "kg_chunk_vectors")
    entity_table = str(raw.get("kg_vector_entity_table") or "kg_entity_vectors")
    relation_table = str(raw.get("kg_vector_relation_table") or "kg_relation_vectors")
    embedding_dim = int(raw.get("embedding_dim") or 1024)

    return MigrationStorageConfig(
        postgres_host=pick("POSTGRES_HOST") or "localhost",
        postgres_port=int(pick("POSTGRES_PORT") or "5432"),
        postgres_user=pick("POSTGRES_USER") or "postgres",
        postgres_password=pick("POSTGRES_PASSWORD") or "",
        postgres_database=pick("POSTGRES_DATABASE") or "postgres",
        neo4j_uri=pick("NEO4J_URI") or "neo4j://localhost:17687",
        neo4j_username=pick("NEO4J_USERNAME") or "neo4j",
        neo4j_password=pick("NEO4J_PASSWORD") or "",
        neo4j_database=pick("NEO4J_DATABASE"),
        kg_schema=kg_schema,
        kg_chunk_table=chunk_table,
        kg_entity_table=entity_table,
        kg_relation_table=relation_table,
        embedding_dim=embedding_dim,
        video_embedding_model=str(raw.get("video_embedding_model") or "").strip() or None,
        video_embedding_dim=int(raw.get("video_embedding_dim") or embedding_dim),
        video_segment_table_base=str(
            raw.get("video_segment_pg_table_base") or "hyperrag_video_segments"
        ),
    )


# ---------------------------------------------------------------------------
# table naming (mirrors hyperrag.video_vdb naming rules)
# ---------------------------------------------------------------------------


def _sanitize_pg_identifier(value: str | None, *, fallback: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value or "")).strip("_").lower()
    if not normalized:
        normalized = fallback
    if normalized[0].isdigit():
        normalized = f"m_{normalized}"
    return normalized


def _fit_pg_identifier(identifier: str, *, max_len: int = 63) -> str:
    if len(identifier) <= max_len:
        return identifier
    digest = hashlib.md5(identifier.encode("utf-8")).hexdigest()[:8]
    return f"{identifier[: max_len - 9]}_{digest}"


def build_video_segment_table_name(
    *, base_table: str, model_name: str | None, vector_dim: int
) -> str:
    base = _sanitize_pg_identifier(base_table, fallback="hyperrag_video_segments")
    dim = int(vector_dim)
    if not str(model_name or "").strip():
        if dim == 1024:
            return _fit_pg_identifier(base)
        return _fit_pg_identifier(f"{base}_d{dim}")
    model = _sanitize_pg_identifier(model_name, fallback="unknown_model")
    return _fit_pg_identifier(f"{base}_{model}_d{dim}")


# ---------------------------------------------------------------------------
# vector literal helpers
# ---------------------------------------------------------------------------


def parse_vector_literal(value: Any) -> list[float]:
    """Parse a pgvector literal (``[1.0,2.0,...]``) into a list of floats."""
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [float(part.strip()) for part in inner.split(",")]
    return []


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


# ---------------------------------------------------------------------------
# PostgreSQL access
# ---------------------------------------------------------------------------


class MigrationPostgres:
    def __init__(self, config: MigrationStorageConfig):
        self.config = config
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await psycopg.AsyncConnection.connect(
            host=self.config.postgres_host,
            port=self.config.postgres_port,
            user=self.config.postgres_user,
            password=self.config.postgres_password,
            dbname=self.config.postgres_database,
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        await self.connect()
        assert self._conn is not None
        async with self._conn.cursor() as cursor:
            await cursor.execute(sql, params or [])
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description or []]
            return [dict(zip(columns, row, strict=False)) for row in rows]

    async def fetch_one(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def execute(
        self, sql: str, params: list[Any] | None = None, *, commit: bool = True
    ) -> None:
        await self.connect()
        assert self._conn is not None
        async with self._conn.cursor() as cursor:
            await cursor.execute(sql, params or [])
        if commit:
            await self._conn.commit()

    async def execute_many(self, sql: str, seq: list[list[Any]]) -> None:
        await self.connect()
        assert self._conn is not None
        async with self._conn.cursor() as cursor:
            for params in seq:
                await cursor.execute(sql, params)
        await self._conn.commit()


# ---------------------------------------------------------------------------
# Neo4j access
# ---------------------------------------------------------------------------


class MigrationNeo4j:
    ENTITY_LABEL = "TLKGEntity"
    RELATION_TYPE = "TLKG_RELATION"

    def __init__(self, config: MigrationStorageConfig):
        self._config = config
        self._driver: Any = None

    async def connect(self) -> None:
        if self._driver is not None:
            return
        from hyperrag.kg.neo4j_driver import create_async_neo4j_driver

        self._driver = create_async_neo4j_driver(
            self._config.neo4j_uri,
            (self._config.neo4j_username, self._config.neo4j_password),
        )

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def fetch_entities(self, workspace: str) -> list[dict[str, Any]]:
        await self.connect()
        assert self._driver is not None
        async with self._driver.session(database=self._config.neo4j_database) as session:
            result = await session.run(
                f"""
                MATCH (n:{self.ENTITY_LABEL})
                WHERE n.workspace = $workspace
                RETURN n {{
                    .workspace, .node_id, .entity_name, .entity_type,
                    .entity_description, .file_path, .status, .library_id,
                    .chunk_ids, .aliases, .normalized_name, .alias_keys,
                    .created_at
                }} AS entity
                """,
                workspace=workspace,
            )
            return [record["entity"] for record in await result.data()]

    async def fetch_relations(self, workspace: str) -> list[dict[str, Any]]:
        await self.connect()
        assert self._driver is not None
        async with self._driver.session(database=self._config.neo4j_database) as session:
            result = await session.run(
                f"""
                MATCH (src:{self.ENTITY_LABEL})-[r:{self.RELATION_TYPE}]->(tgt:{self.ENTITY_LABEL})
                WHERE r.workspace = $workspace
                RETURN src.node_id AS source_node_id, tgt.node_id AS target_node_id, r {{
                    .workspace, .relation_id, .source_entity, .target_entity,
                    .relationship_description, .file_path, .chunk_ids, .status,
                    .library_id
                }} AS relation
                """,
                workspace=workspace,
            )
            return [record for record in await result.data()]

    async def merge_entities(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        await self.connect()
        assert self._driver is not None
        async with self._driver.session(database=self._config.neo4j_database) as session:
            await session.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:{self.ENTITY_LABEL} {{workspace: row.workspace, node_id: row.node_id}})
                ON CREATE SET
                    n.entity_name = row.entity_name,
                    n.entity_type = row.entity_type,
                    n.entity_description = row.entity_description,
                    n.file_path = row.file_path,
                    n.status = row.status,
                    n.library_id = row.library_id,
                    n.chunk_ids = row.chunk_ids,
                    n.aliases = row.aliases,
                    n.normalized_name = row.normalized_name,
                    n.alias_keys = row.alias_keys,
                    n.created_at = row.created_at
                ON MATCH SET
                    n.entity_name = row.entity_name,
                    n.entity_type = row.entity_type,
                    n.entity_description = row.entity_description,
                    n.file_path = row.file_path,
                    n.status = row.status,
                    n.library_id = row.library_id,
                    n.chunk_ids = row.chunk_ids,
                    n.aliases = row.aliases,
                    n.normalized_name = row.normalized_name,
                    n.alias_keys = row.alias_keys
                """,
                rows=[_json_compatible(row) for row in rows],
            )

    async def merge_relations(self, rows: list[dict[str, Any]]) -> None:
        """Merge relation edges using source/target node ids captured at export.

        Each row must carry ``source_node_id`` and ``target_node_id`` in addition
        to the relation's own fields; the relation is merged on
        ``(workspace, relation_id)``.
        """
        if not rows:
            return
        await self.connect()
        assert self._driver is not None
        async with self._driver.session(database=self._config.neo4j_database) as session:
            source_match = (
                f"MATCH (src:{self.ENTITY_LABEL} "
                "{workspace: row.workspace, node_id: row.source_node_id})"
            )
            target_match = (
                f"MATCH (tgt:{self.ENTITY_LABEL} "
                "{workspace: row.workspace, node_id: row.target_node_id})"
            )
            merge_edge = (
                f"MERGE (src)-[r:{self.RELATION_TYPE} "
                "{workspace: row.workspace, relation_id: row.relation_id}]->(tgt)"
            )
            await session.run(
                "\n".join(
                    [
                        "UNWIND $rows AS row",
                        source_match,
                        target_match,
                        merge_edge,
                        "ON CREATE SET",
                        "    r.source_entity = row.source_entity,",
                        "    r.target_entity = row.target_entity,",
                        "    r.relationship_description = row.relationship_description,",
                        "    r.file_path = row.file_path,",
                        "    r.chunk_ids = row.chunk_ids,",
                        "    r.status = row.status,",
                        "    r.library_id = row.library_id",
                        "ON MATCH SET",
                        "    r.source_entity = row.source_entity,",
                        "    r.target_entity = row.target_entity,",
                        "    r.relationship_description = row.relationship_description,",
                        "    r.file_path = row.file_path,",
                        "    r.chunk_ids = row.chunk_ids,",
                        "    r.status = row.status,",
                        "    r.library_id = row.library_id",
                    ]
                ),
                rows=[_json_compatible(row) for row in rows],
            )


def _json_compatible(value: Any) -> Any:
    """Encode values (lists of strings, etc.) into JSON-compatible primitives."""
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_compatible(v) for k, v in value.items()}
    return value
