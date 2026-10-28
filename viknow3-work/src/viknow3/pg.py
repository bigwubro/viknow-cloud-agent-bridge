"""One Postgres server. Each business has its own database, and its tables live in a named schema.

``public`` keeps extensions (``vector``, ``pg_trgm``). Application search paths are
``<schema>, public`` so those types stay visible.

| Database | Schema | What |
| --- | --- | --- |
| ``viknow3_access`` | ``access`` | users, sessions, files, runs |
| ``viknow3_agent`` | ``checkpoint`` | LangGraph checkpoint tables |
| ``viknow3_agent`` | ``adk`` | ADK session tables |
| ``viknow3_knowledge`` | ``catalog`` | knowledge libraries, assets, index jobs |
| ``viknow3_knowledge`` | ``hyperrag`` | HyperRAG status, chunks, discovery |
| ``viknow3_knowledge`` | ``hyperrag_kg`` | vector tables |
| ``viknow3_ops`` | ``ops`` | platform configuration |
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ACCESS = "access"
AGENT = "agent"
HYPERRAG = "hyperrag"

ACCESS_SCHEMA = "access"
CHECKPOINT_SCHEMA = "checkpoint"
ADK_SCHEMA = "adk"
CATALOG_SCHEMA = "catalog"
HYPERRAG_SCHEMA = "hyperrag"
OPS_SCHEMA = "ops"

_ENV = {
    ACCESS: "VIKNOW_ACCESS_DSN",
    AGENT: "VIKNOW_AGENT_DSN",
    HYPERRAG: "VIKNOW_HYPERRAG_DSN",
}

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def dsn(business: str) -> str:
    key = _ENV[business]
    value = os.environ.get(key, "").strip()
    if not value:
        raise RuntimeError(f"{key} is required")
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value[len("postgresql+psycopg://") :]
    return value


def sqlalchemy_url(business: str) -> str:
    value = dsn(business)
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


def with_search_path(url: str, schema: str) -> str:
    """Return ``url`` with libpq ``search_path=<schema>, public``."""

    _check_schema(schema)
    text = url.strip()
    if not text:
        return text
    parts = urlsplit(text)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    option = f"-csearch_path={schema},public"
    existing = query.get("options", "").strip()
    if option not in existing:
        query["options"] = f"{existing} {option}".strip() if existing else option
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def connect(business: str, *, schema: str | None = None) -> Any:
    import psycopg

    conn = psycopg.connect(dsn(business), autocommit=True)
    if schema:
        use_schema(conn, schema)
    return conn


def ensure_schema(business: str, schema: str) -> None:
    conn = connect(business)
    try:
        use_schema(conn, schema)
    finally:
        conn.close()


def use_schema(conn: Any, schema: str) -> None:
    _check_schema(schema)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    conn.execute(f"SET search_path TO {schema}, public")


def database_name(business: str) -> str:
    return dsn(business).rsplit("/", 1)[-1].split("?", 1)[0]


def require_test_database(business: str) -> None:
    name = database_name(business)
    if not name.endswith("_test"):
        raise RuntimeError(f"{business} database {name} is not a test database")


def _check_schema(schema: str) -> None:
    if not _SCHEMA_NAME.match(schema):
        raise RuntimeError(f"invalid schema {schema}")
