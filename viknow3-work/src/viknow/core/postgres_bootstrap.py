"""Bootstrap ViKnow Postgres schemas before persistence backends connect."""

from __future__ import annotations

import os

import psycopg
from psycopg import sql

from viknow.core.config import (
    DEFAULT_CHECKPOINT_DSN_ENV,
    DEFAULT_MEMORY_DSN_ENV,
    DEFAULT_SESSION_DSN_ENV,
    PersistenceBackend,
    PersistenceConfig,
    PostgresPersistenceConfig,
)


def ensure_viknow_postgres_schemas(persistence: PersistenceConfig) -> list[str]:
    """Create configured ViKnow schemas when missing.

    Uses ``persistence.postgres`` from app.yaml. Skips when explicit
    ``VIKNOW_*_DSN`` URLs are set. Returns the list of schemas that were created.
    """

    if persistence.backend != PersistenceBackend.POSTGRES:
        return []
    if _using_explicit_postgres_dsns():
        return []
    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required when backend is postgres")

    schema_names = list_viknow_postgres_schema_names(persistence)
    if not schema_names:
        return []

    postgres = persistence.postgres
    if (
        postgres.host is None
        or postgres.port is None
        or postgres.user is None
        or postgres.password is None
        or postgres.database is None
    ):
        raise RuntimeError(
            "persistence.postgres connection fields incomplete; "
            "set them in app.yaml or hyperrag.yml env.POSTGRES_*"
        )
    created: list[str] = []
    with psycopg.connect(
        host=postgres.host,
        port=postgres.port,
        user=postgres.user,
        password=postgres.password,
        dbname=postgres.database,
        autocommit=True,
    ) as conn:
        for schema_name in schema_names:
            if _schema_exists(conn, schema_name):
                continue
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
            created.append(schema_name)
        _ensure_pgvector_in_public(conn)
    return created


def ensure_juicefs_meta_schema(postgres: PostgresPersistenceConfig) -> str | None:
    """Create the JuiceFS metadata schema when missing.

    Independent of ``persistence.backend`` (session may stay on sqlite while
    JuiceFS still uses Postgres for meta).
    """

    if (
        postgres.host is None
        or postgres.port is None
        or postgres.user is None
        or postgres.password is None
        or postgres.database is None
    ):
        raise RuntimeError("JuiceFS Postgres connection fields incomplete for JuiceFS meta schema")
    schema_name = postgres.juicefs_schema
    with psycopg.connect(
        host=postgres.host,
        port=postgres.port,
        user=postgres.user,
        password=postgres.password,
        dbname=postgres.database,
        autocommit=True,
    ) as conn:
        if _schema_exists(conn, schema_name):
            return None
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    return schema_name


def ensure_learning_postgres_schema(persistence: PersistenceConfig) -> str | None:
    """Create the shared schema used by learning projections and human case review."""

    if persistence.backend != PersistenceBackend.POSTGRES:
        return None
    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required for learning schema")
    postgres = persistence.postgres
    if (
        postgres.host is None
        or postgres.port is None
        or postgres.user is None
        or postgres.password is None
        or postgres.database is None
    ):
        raise RuntimeError("persistence.postgres connection fields incomplete for learning schema")
    schema_name = postgres.learning_schema
    with psycopg.connect(
        host=postgres.host,
        port=postgres.port,
        user=postgres.user,
        password=postgres.password,
        dbname=postgres.database,
        autocommit=True,
    ) as conn:
        if _schema_exists(conn, schema_name):
            return None
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    return schema_name


def list_viknow_postgres_schema_names(persistence: PersistenceConfig) -> list[str]:
    """Return configured ViKnow schema names in stable order."""

    if persistence.postgres is None:
        raise RuntimeError("persistence.postgres is required to list schema names")
    postgres = persistence.postgres
    return [
        postgres.session_schema,
        postgres.checkpoint_schema,
        postgres.memory_schema,
        postgres.scheduler_schema,
        postgres.learning_schema,
    ]


def _using_explicit_postgres_dsns() -> bool:
    return any(
        os.getenv(env_name) and os.getenv(env_name, "").strip()
        for env_name in (
            DEFAULT_SESSION_DSN_ENV,
            DEFAULT_CHECKPOINT_DSN_ENV,
            DEFAULT_MEMORY_DSN_ENV,
        )
    )


def _schema_exists(conn: psycopg.Connection, schema_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema_name,),
    ).fetchone()
    return row is not None


def _ensure_pgvector_in_public(conn: psycopg.Connection) -> None:
    """Keep pgvector in ``public`` so it can be shared across co-located services."""

    row = conn.execute(
        """
        SELECT n.nspname
        FROM pg_extension AS e
        JOIN pg_namespace AS n ON e.extnamespace = n.oid
        WHERE e.extname = 'vector'
        """
    ).fetchone()
    if row is None:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return
    if row[0] == "public":
        return
    conn.execute("ALTER EXTENSION vector SET SCHEMA public")
