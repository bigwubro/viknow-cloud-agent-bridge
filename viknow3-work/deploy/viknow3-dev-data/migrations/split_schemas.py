"""Move application tables out of public and rename databases to viknow3_*.

Idempotent. Run on the Postgres host before starting a process that sets the
new search_path. Reads DSNs from the environment and does not print them.
The role name ``viknow_ops`` is left unchanged.

    VIKNOW_ACCESS_DSN=... VIKNOW_AGENT_DSN=... \\
    VIKNOW_HYPERRAG_DSN=... VIKNOW_OPS_DSN=... \\
    python deploy/viknow3-dev-data/migrations/split_schemas.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit, urlunsplit

ACCESS_TABLES = (
    "files",
    "run_events",
    "run_files",
    "run_refs",
    "sessions",
    "users",
    "workspaces",
)
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)
ADK_TABLES = (
    "sessions",
    "events",
    "app_states",
    "user_states",
    "adk_internal_metadata",
)
CATALOG_TABLES = (
    "knowledge_libraries",
    "knowledge_assets",
    "knowledge_index_jobs",
)
HYPERRAG_TABLES = (
    "hyperrag_asset_status",
    "hyperrag_bm25_chunks",
    "hyperrag_knowledgebase_info",
    "hyperrag_knowledge_discovery_libraries",
    "hyperrag_knowledge_discovery_assets",
)
OPS_TABLES = (
    "model_provider",
    "model",
    "agent_model",
    "postgres_connection",
    "redis_connection",
    "neo4j_connection",
    "s3_client",
    "retrieval_setting",
    "index_setting",
    "observe_setting",
    "service_setting",
    "config_change",
)

RENAMES = (
    ("viknow_access", "viknow3_access"),
    ("viknow_access_test", "viknow3_access_test"),
    ("viknow_agent", "viknow3_agent"),
    ("viknow_agent_test", "viknow3_agent_test"),
    ("viknow_ops", "viknow3_ops"),
    ("viknow_ops_test", "viknow3_ops_test"),
)


def main() -> int:
    import psycopg

    notes: list[str] = []
    for key, mover in (
        ("VIKNOW_ACCESS_DSN", _move_access),
        ("VIKNOW_AGENT_DSN", _move_agent),
        ("VIKNOW_HYPERRAG_DSN", _move_knowledge),
        ("VIKNOW_OPS_DSN", _move_ops),
    ):
        url = os.environ.get(key, "").strip()
        if not url:
            notes.append(f"skip {key}: unset")
            continue
        url = _normalize(url)
        _with_db(psycopg, url, mover, notes)
        test_url = _test_sibling(url)
        if test_url and _database_exists(psycopg, url, _db_name(test_url)):
            _with_db(psycopg, test_url, mover, notes)
    admin = _admin_url(os.environ.get("VIKNOW_ACCESS_DSN") or os.environ.get("VIKNOW_OPS_DSN") or "")
    if not admin:
        notes.append("skip rename: no DSN for the postgres database")
    else:
        _rename_databases(psycopg, _normalize(admin), notes)
    print("\n".join(notes))
    failed = [line for line in notes if line.startswith("conflict")]
    return 1 if failed else 0


def _move_access(conn, notes: list[str]) -> None:
    _ensure_schema(conn, "access")
    for table in ACCESS_TABLES:
        notes.append(_move_table(conn, table, "access"))


def _move_agent(conn, notes: list[str]) -> None:
    _ensure_schema(conn, "checkpoint")
    for table in CHECKPOINT_TABLES:
        notes.append(_move_table(conn, table, "checkpoint"))
    _ensure_schema(conn, "adk")
    _ensure_schema(conn, "retired")
    for table in ADK_TABLES:
        notes.append(_move_adk_table(conn, table))


def _move_knowledge(conn, notes: list[str]) -> None:
    _ensure_schema(conn, "catalog")
    _ensure_schema(conn, "hyperrag")
    for table in CATALOG_TABLES:
        notes.append(_move_table(conn, table, "catalog"))
    for table in HYPERRAG_TABLES:
        notes.append(_move_table(conn, table, "hyperrag"))
    _pin_extensions(conn)


def _move_ops(conn, notes: list[str]) -> None:
    if _schema_exists(conn, "viknow_ops") and not _schema_exists(conn, "ops"):
        conn.execute("ALTER SCHEMA viknow_ops RENAME TO ops")
        notes.append(f"{conn.info.dbname}: renamed schema viknow_ops to ops")
        return
    _ensure_schema(conn, "ops")
    for table in OPS_TABLES:
        if _table_exists(conn, "viknow_ops", table):
            notes.append(_move_table(conn, table, "ops", source_schema="viknow_ops"))
        else:
            notes.append(_move_table(conn, table, "ops"))
    if _schema_exists(conn, "viknow_ops") and _schema_empty(conn, "viknow_ops"):
        conn.execute("DROP SCHEMA viknow_ops")
        notes.append(f"{conn.info.dbname}: dropped empty schema viknow_ops")


def _move_table(conn, table: str, dest: str, *, source_schema: str = "public") -> str:
    db = conn.info.dbname
    if not _table_exists(conn, source_schema, table):
        return f"{db}: {source_schema}.{table} already not in source"
    if _table_exists(conn, dest, table):
        src = _count(conn, source_schema, table)
        dst = _count(conn, dest, table)
        if src == 0:
            conn.execute(f"DROP TABLE {source_schema}.{table} CASCADE")
            return f"{db}: dropped empty {source_schema}.{table}"
        if dst == 0:
            conn.execute(f"DROP TABLE {dest}.{table} CASCADE")
        else:
            return f"conflict {db}: {source_schema}.{table}={src} {dest}.{table}={dst}"
    conn.execute(f"ALTER TABLE {source_schema}.{table} SET SCHEMA {dest}")
    return f"{db}: {source_schema}.{table} -> {dest}.{table}"


def _move_adk_table(conn, table: str) -> str:
    db = conn.info.dbname
    if not _table_exists(conn, "public", table):
        return f"{db}: public.{table} already not in public"
    if not _table_exists(conn, "adk", table):
        conn.execute(f"ALTER TABLE public.{table} SET SCHEMA adk")
        return f"{db}: public.{table} -> adk.{table}"
    src = _count(conn, "public", table)
    if src == 0:
        conn.execute(f"DROP TABLE public.{table} CASCADE")
        return f"{db}: dropped empty public.{table}; kept adk.{table}"
    parked = f"public_{table}"
    if _table_exists(conn, "retired", parked):
        return f"conflict {db}: retired.{parked} already exists beside public.{table}={src}"
    conn.execute(f"ALTER TABLE public.{table} RENAME TO {parked}")
    conn.execute(f"ALTER TABLE public.{parked} SET SCHEMA retired")
    return f"{db}: public.{table}={src} parked at retired.{parked}; kept adk.{table}"


def _pin_extensions(conn) -> None:
    for name in ("vector", "pg_trgm"):
        row = conn.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s",
            (name,),
        ).fetchone()
        if row is None or row[0] == "public":
            continue
        conn.execute(f"ALTER EXTENSION {name} SET SCHEMA public")


def _rename_databases(psycopg, admin_url: str, notes: list[str]) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for old, new in RENAMES:
            old_exists = _database_exists(psycopg, admin_url, old, conn=conn)
            new_exists = _database_exists(psycopg, admin_url, new, conn=conn)
            if old_exists and new_exists:
                notes.append(f"conflict rename {old}: {new} already exists")
                continue
            if not old_exists:
                notes.append(f"rename {old}: already absent")
                continue
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (old,),
            )
            conn.execute(f"ALTER DATABASE {old} RENAME TO {new}")
            notes.append(f"renamed database {old} -> {new}")
        for leftover in ("viknow_hyperrag", "viknow_hyperrag_test"):
            if _database_exists(psycopg, admin_url, leftover, conn=conn):
                notes.append(f"left database {leftover}")


def _with_db(psycopg, url: str, mover, notes: list[str]) -> None:
    name = _db_name(url)
    if not _database_exists(psycopg, url, name):
        notes.append(f"skip {name}: database absent")
        return
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET lock_timeout = '15s'")
        mover(conn, notes)


def _database_exists(psycopg, url: str, name: str, conn=None) -> bool:
    if conn is not None:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None
    admin = _admin_url(url)
    with psycopg.connect(admin, autocommit=True) as owned:
        row = owned.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None


def _ensure_schema(conn, schema: str) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def _schema_exists(conn, schema: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
    return row is not None


def _schema_empty(conn, schema: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
        (schema,),
    ).fetchone()
    return row is None


def _table_exists(conn, schema: str, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()
    return row is not None and row[0] is not None


def _count(conn, schema: str, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()
    return int(row[0])


def _normalize(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _db_name(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _admin_url(url: str) -> str:
    url = _normalize(url.strip())
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def _test_sibling(url: str) -> str:
    parts = urlsplit(url)
    name = parts.path.rsplit("/", 1)[-1]
    if name.endswith("_test"):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}_test", "", ""))


if __name__ == "__main__":
    sys.exit(main())
