"""Explicit, offline operations for the two discovery tables only."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import asyncpg

from .discovery_tokens import discovery_token_version
from .knowledge_discovery import SQL_TEMPLATES_KNOWLEDGE_DISCOVERY, KnowledgeDiscoveryCatalogStorage

TABLES = (
    "hyperrag_knowledge_discovery_libraries",
    "hyperrag_knowledge_discovery_assets",
)


def qualified_table(schema: str, table: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or table not in TABLES:
        raise ValueError("Invalid discovery maintenance object")
    return f'"{schema}"."{table}"'


async def catalog_state(conn: asyncpg.Connection, *, schema: str = "public") -> dict[str, Any]:
    identity = dict(
        await conn.fetchrow(
            "SELECT current_database() AS database, inet_server_addr()::text AS server_address, "
            "inet_server_port() AS server_port, pg_is_in_recovery() AS recovery",
        )
    )
    tables = []
    for table in TABLES:
        qualified = qualified_table(schema, table)
        size = dict(
            await conn.fetchrow(
                """SELECT c.oid::bigint AS oid, c.relowner::bigint AS owner, c.relacl::text AS acl,
               pg_relation_size(c.oid) AS heap_bytes, pg_table_size(c.oid) AS table_bytes,
               pg_indexes_size(c.oid) AS index_bytes, pg_total_relation_size(c.oid) AS total_bytes
               FROM pg_class c WHERE c.oid=$1::regclass""",
                qualified,
            )
        )
        keys = "library_id, asset_id" if table == TABLES[1] else "library_id"
        content = dict(
            await conn.fetchrow(
                f"""SELECT count(*) AS rows,
                coalesce(sum(pg_column_size(t)),0)::bigint AS row_bytes,
                encode(sha256(convert_to(coalesce(string_agg(
                  (to_jsonb(t)-'search_tokens'-'search_version')::text,
                  E'\\n' ORDER BY {keys}),''),'UTF8')),'hex') AS content_sha256
                FROM {qualified} t""",
            )
        )
        constraints = [
            dict(row)
            for row in await conn.fetch(
                """SELECT conname, pg_get_constraintdef(oid) AS definition
               FROM pg_constraint WHERE conrelid=$1::regclass ORDER BY conname""",
                qualified,
            )
        ]
        indexes = [
            row["definition"]
            for row in await conn.fetch(
                """SELECT pg_get_indexdef(indexrelid) AS definition FROM pg_index
               WHERE indrelid=$1::regclass ORDER BY indexrelid::regclass::text""",
                qualified,
            )
        ]
        tables.append(
            {"name": table, **size, **content, "constraints": constraints, "indexes": indexes}
        )
    return {
        "identity": identity,
        "schema": schema,
        "tables": tables,
        "data_directory": await conn.fetchval("SHOW data_directory"),
    }


def assert_target(state: dict[str, Any], *, database: str, oids: list[int]) -> None:
    if state["identity"]["database"] != database or state["identity"]["recovery"]:
        raise RuntimeError("Maintenance database identity does not match approval")
    if [table["oid"] for table in state["tables"]] != oids:
        raise RuntimeError("Maintenance table identities do not match approval")


def assert_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before["identity"] != after["identity"] or before["schema"] != after["schema"]:
        raise RuntimeError("Maintenance target changed")
    for old, new in zip(before["tables"], after["tables"], strict=True):
        for field in ("name", "oid", "owner", "acl", "rows", "content_sha256"):
            if old[field] != new[field]:
                raise RuntimeError(f"Catalog maintenance changed {old['name']} {field}")
        if not set(old["indexes"]).issubset(new["indexes"]):
            raise RuntimeError(f"Catalog maintenance lost an index on {old['name']}")
        previous = {(item["conname"], item["definition"]) for item in old["constraints"]}
        current = {(item["conname"], item["definition"]) for item in new["constraints"]}
        if not previous.issubset(current):
            raise RuntimeError(f"Catalog maintenance lost a constraint on {old['name']}")


async def migrate_tokens(conn: asyncpg.Connection, *, schema: str = "public") -> None:
    """Preserve business timestamps and content; all token changes commit atomically."""
    async with conn.transaction():
        await conn.execute(
            "LOCK TABLE "
            + ", ".join(qualified_table(schema, table) for table in TABLES)
            + " IN ACCESS EXCLUSIVE MODE"
        )
        for table in TABLES:
            qualified = qualified_table(schema, table)
            await conn.execute(
                f"ALTER TABLE {qualified} "
                "ADD COLUMN IF NOT EXISTS search_tokens TEXT[] NOT NULL DEFAULT '{}', "
                "ADD COLUMN IF NOT EXISTS search_version TEXT NOT NULL DEFAULT ''",
            )
            rows = await conn.fetch(f"SELECT * FROM {qualified}")
            values = []
            for row in rows:
                if table == TABLES[0]:
                    item = KnowledgeDiscoveryCatalogStorage._to_library(dict(row))
                    projected = replace(
                        item, search_tokens=None, search_version=discovery_token_version()
                    )
                    values.append(
                        (list(projected.search_tokens), projected.search_version, item.library_id)
                    )
                else:
                    item = KnowledgeDiscoveryCatalogStorage._to_asset(dict(row))
                    projected = replace(
                        item, search_tokens=None, search_version=discovery_token_version()
                    )
                    values.append(
                        (
                            list(projected.search_tokens),
                            projected.search_version,
                            item.library_id,
                            item.asset_id,
                        )
                    )
            if values:
                key = "library_id=$3" + (" AND asset_id=$4" if table == TABLES[1] else "")
                await conn.executemany(
                    f"UPDATE {qualified} SET search_tokens=$1, search_version=$2 "
                    f"WHERE {key} AND (search_tokens,search_version) "
                    "IS DISTINCT FROM ($1::text[],$2::text)",
                    values,
                )


async def compact_tables(conn: asyncpg.Connection, *, schema: str = "public") -> None:
    if conn.is_in_transaction():
        raise RuntimeError("VACUUM FULL must not run inside a transaction")
    for table in TABLES:
        await conn.execute(f"VACUUM (FULL, ANALYZE) {qualified_table(schema, table)}")


async def build_indexes(conn: asyncpg.Connection, *, schema: str = "public") -> None:
    for sql in SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["create_indexes"]:
        for table in TABLES:
            sql = sql.replace(f"ON {table}", f"ON {qualified_table(schema, table)}")
        await conn.execute(sql)
    for table in TABLES:
        await conn.execute(f"ANALYZE {qualified_table(schema, table)}")


async def verify_tokens(conn: asyncpg.Connection, *, schema: str = "public") -> None:
    for table in TABLES:
        rows = await conn.fetch(f"SELECT * FROM {qualified_table(schema, table)}")
        for row in rows:
            item = (
                KnowledgeDiscoveryCatalogStorage._to_library(dict(row))
                if table == TABLES[0]
                else KnowledgeDiscoveryCatalogStorage._to_asset(dict(row))
            )
            expected = replace(item, search_tokens=None, search_version=discovery_token_version())
            if (
                item.search_tokens != expected.search_tokens
                or item.search_version != expected.search_version
            ):
                raise RuntimeError(f"Catalog token verification failed for {table}")
