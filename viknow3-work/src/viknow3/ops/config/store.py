"""Read and write viknow_ops. One Postgres instance, one database per environment."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg import Connection
from psycopg.errors import ForeignKeyViolation
from psycopg.rows import dict_row

from viknow3.clock import new_id, now, stamp
from viknow3.ops.config.catalog import ROW_KEY, SINGLETONS, TABLES, Column

SCHEMA = "viknow_ops"


class ConfigError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def configured() -> bool:
    return bool(os.environ.get("VIKNOW_OPS_DSN"))


def snapshot() -> dict[str, Any]:
    with _session() as conn:
        return _read_all(conn)


def set_value(table: str, column: str, value: Any, row_id: str | None, actor: str) -> dict[str, Any]:
    spec = _column(table, column)
    if spec.secret and (value is None or value == ""):
        return _row_or_missing(table, row_id)
    stored = _encrypt(_coerce(spec, value)) if spec.secret else _coerce(spec, value)
    with _session() as conn:
        key = _row_key(table)
        current = _find(conn, table, row_id)
        if current is None:
            raise ConfigError(404, {"code": "config_row_missing", "message": "config row not found"})
        target_id = None if key is None else str(current[key])
        previous = None if spec.secret else _plain(current.get(column))
        try:
            _update(conn, table, key, target_id or "", column, stored)
        except ForeignKeyViolation as exc:
            raise ConfigError(400, {"code": "invalid_reference", "message": "value does not match a row"}) from exc
        _insert_change(conn, table, column, target_id, previous, spec.secret, actor)
        updated = _find(conn, table, target_id)
    return _mask_row(table, updated or {})


def changes(limit: int = 50) -> list[dict[str, Any]]:
    with _session() as conn:
        rows = conn.execute(
            """
            SELECT id, target_table, target_column, target_id, previous_value, secret, actor, created_at
            FROM config_change
            WHERE deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [_change_public(row) for row in rows]


def revert(change_id: str, actor: str) -> dict[str, Any]:
    with _session() as conn:
        row = conn.execute(
            """
            SELECT id, target_table, target_column, target_id, previous_value, secret
            FROM config_change
            WHERE id = %s AND deleted_at IS NULL
            """,
            (change_id,),
        ).fetchone()
        if row is None:
            raise ConfigError(404, {"code": "change_missing", "message": "change not found"})
        if row["secret"]:
            raise ConfigError(400, {"code": "secret_not_restored", "message": "a secret change has no previous value"})
        spec = _column(row["target_table"], row["target_column"])
        value = _parse_previous(spec, row["previous_value"])
        key = _row_key(row["target_table"])
        current = _find(conn, row["target_table"], row["target_id"])
        if current is None:
            raise ConfigError(404, {"code": "config_row_missing", "message": "config row not found"})
        target_id = None if key is None else str(current[key])
        previous = _plain(current.get(row["target_column"]))
        _update(conn, row["target_table"], key, target_id or "", row["target_column"], value)
        _insert_change(conn, row["target_table"], row["target_column"], target_id, previous, False, actor)
    return snapshot()


def agent_model_map() -> dict[str, str]:
    if not configured():
        return {}
    with _session() as conn:
        rows = conn.execute(
            "SELECT agent_id, model_id FROM agent_model WHERE deleted_at IS NULL"
        ).fetchall()
    return {row["agent_id"]: row["model_id"] for row in rows}


def list_models() -> list[dict[str, str]]:
    if not configured():
        return []
    with _session() as conn:
        rows = conn.execute(
            "SELECT id, label FROM model WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
    return [{"id": row["id"], "label": row["label"]} for row in rows]


def known_model(model_id: str) -> bool:
    if not configured():
        return False
    with _session() as conn:
        row = conn.execute(
            "SELECT id FROM model WHERE id = %s AND deleted_at IS NULL",
            (model_id,),
        ).fetchone()
    return row is not None


def server_default_model() -> str | None:
    """Model id used when an agent and a run both leave the model empty."""
    if not configured():
        return None
    with _session() as conn:
        provider = conn.execute(
            "SELECT model_name FROM model_provider WHERE id = 'private' AND deleted_at IS NULL"
        ).fetchone()
        if provider is None or not provider["model_name"]:
            return None
        found = conn.execute(
            """
            SELECT id FROM model
            WHERE provider_id = 'private' AND name = %s AND deleted_at IS NULL
            """,
            (provider["model_name"],),
        ).fetchone()
    return None if found is None else found["id"]


def reset_for_tests() -> None:
    dsn = os.environ.get("VIKNOW_OPS_DSN") or ""
    if not dsn.rstrip("/").endswith("/viknow_ops_test"):
        raise ConfigError(400, {"code": "refusing_reset", "message": "reset only runs against viknow_ops_test"})
    with _session() as conn:
        conn.execute(
            """
            TRUNCATE
                agent_model, model, model_provider,
                postgres_connection, redis_connection, neo4j_connection, s3_client,
                retrieval_setting, index_setting, observe_setting, service_setting,
                config_change
            CASCADE
            """
        )
        _seed(conn)


def _row_or_missing(table: str, row_id: str | None) -> dict[str, Any]:
    with _session() as conn:
        found = _find(conn, table, row_id)
    if found is None:
        raise ConfigError(404, {"code": "config_row_missing", "message": "config row not found"})
    return _mask_row(table, found)


@contextmanager
def _session() -> Iterator[Connection]:
    conn = _connect()
    try:
        _ensure(conn)
        yield conn
        conn.commit()
    except ConfigError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _connect() -> Connection:
    dsn = os.environ.get("VIKNOW_OPS_DSN")
    if not dsn:
        raise ConfigError(503, {"code": "ops_dsn_missing", "message": "VIKNOW_OPS_DSN is not set"})
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except Exception as exc:
        raise ConfigError(503, {"code": "ops_unavailable", "message": "ops database is unavailable"}) from exc
    return conn


def _ensure(conn: Connection) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.execute(f"SET search_path TO {SCHEMA}")
    for statement in _STATEMENTS:
        conn.execute(statement)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (81432026,))
    count = conn.execute("SELECT count(*) AS n FROM model_provider WHERE deleted_at IS NULL").fetchone()
    if count and count["n"] == 0:
        _seed(conn)


def _seed(conn: Connection) -> None:
    moment = now()
    providers = [
        ("private", "http://127.0.0.1:8443/v1", "vllm/Qwen/Qwen3.6-35B-A3B-FP8"),
        ("siliconflow", "https://api.siliconflow.cn/v1", None),
        ("embedding", "http://127.0.0.1:8601/v1", "Qwen3-VL-Embedding-2B"),
        ("rerank", "http://127.0.0.1:8602/v1", "Qwen3-VL-Reranker-2B"),
        ("actio", "http://127.0.0.1:8600", None),
    ]
    for provider_id, base_url, model_name in providers:
        conn.execute(
            """
            INSERT INTO model_provider (id, base_url, model_name, api_key, created_at, updated_at)
            VALUES (%s, %s, %s, NULL, %s, %s)
            """,
            (provider_id, base_url, model_name, moment, moment),
        )
    models = [
        ("private:qwen", "private", "vllm/Qwen/Qwen3.6-35B-A3B-FP8", "内网 Qwen"),
        ("siliconflow:Qwen/Qwen3.6-35B-A3B", "siliconflow", "Qwen/Qwen3.6-35B-A3B", "Qwen3.6-35B-A3B"),
    ]
    for model_id, provider_id, name, label in models:
        conn.execute(
            """
            INSERT INTO model (id, provider_id, name, label, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (model_id, provider_id, name, label, moment, moment),
        )
    for agent_id, model_id in (("expertbuddy-deepagents", "private:qwen"), ("expertbuddy-adk", "private:qwen")):
        conn.execute(
            """
            INSERT INTO agent_model (agent_id, model_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s)
            """,
            (agent_id, model_id, moment, moment),
        )
    conn.execute(
        """
        INSERT INTO postgres_connection (id, host, port, "user", password, database, created_at, updated_at)
        VALUES ('default', '127.0.0.1', 16432, 'viknow_ops', NULL, 'viknow3_knowledge', %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO redis_connection (id, host, port, username, database, password, created_at, updated_at)
        VALUES ('default', '127.0.0.1', 16479, 'default', 0, NULL, %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO neo4j_connection (id, uri, username, password, created_at, updated_at)
        VALUES ('default', 'neo4j://127.0.0.1:17487', 'neo4j', NULL, %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO s3_client (
            id, endpoint, public_endpoint, bucket, region, url_expires_seconds,
            access_key, secret_key, created_at, updated_at
        )
        VALUES ('default', '', '', '', 'us-east-1', 3600, NULL, NULL, %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO retrieval_setting (id, library, mode, top_k, rerank_enabled, created_at, updated_at)
        VALUES ('default', 'default', 'hybrid', 4, TRUE, %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO index_setting (
            id, segmentation, asr_index, asr_qa, ocr, mineru_model, created_at, updated_at
        )
        VALUES ('default', 'semantic', 'qwen_vad', 'qwen_vad', 'paddleocr', 'vllm/mineru', %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO observe_setting (
            id, langfuse_enabled, langfuse_host, langfuse_public_key, langfuse_secret_key,
            tracing_enabled, media_enabled, otel_enabled, otel_endpoint,
            usage_enabled, usage_url, usage_token, created_at, updated_at
        )
        VALUES ('default', FALSE, '', NULL, NULL, FALSE, FALSE, FALSE, '', FALSE, '', NULL, %s, %s)
        """,
        (moment, moment),
    )
    conn.execute(
        """
        INSERT INTO service_setting (id, log_level, admin_username, admin_password, created_at, updated_at)
        VALUES ('default', 'info', 'admin', NULL, %s, %s)
        """,
        (moment, moment),
    )


def _read_all(conn: Connection) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for table, key in ROW_KEY.items():
        rows = conn.execute(f"SELECT * FROM {table} WHERE deleted_at IS NULL ORDER BY {key}").fetchall()
        body[table] = [_mask_row(table, row) for row in rows]
    for table in SINGLETONS:
        row = conn.execute(f"SELECT * FROM {table} WHERE deleted_at IS NULL").fetchone()
        body[table] = None if row is None else _mask_row(table, row)
    return body


def _find(conn: Connection, table: str, row_id: str | None) -> dict[str, Any] | None:
    key = _row_key(table)
    if key is None:
        return conn.execute(f"SELECT * FROM {table} WHERE deleted_at IS NULL").fetchone()
    if not row_id:
        raise ConfigError(400, {"code": "row_id_required", "message": "this table needs an id"})
    return conn.execute(
        f"SELECT * FROM {table} WHERE {key} = %s AND deleted_at IS NULL",
        (row_id,),
    ).fetchone()


def _sql_column(column: str) -> str:
    if column == "user":
        return '"user"'
    return column


def _update(conn: Connection, table: str, key: str | None, row_id: str, column: str, value: Any) -> None:
    col = _sql_column(column)
    if key is None:
        conn.execute(
            f"UPDATE {table} SET {col} = %s, updated_at = %s WHERE deleted_at IS NULL",
            (value, now()),
        )
        return
    conn.execute(
        f"UPDATE {table} SET {col} = %s, updated_at = %s WHERE {key} = %s AND deleted_at IS NULL",
        (value, now(), row_id),
    )


def _insert_change(
    conn: Connection,
    table: str,
    column: str,
    row_id: str | None,
    previous: str | None,
    secret: bool,
    actor: str,
) -> None:
    moment = now()
    conn.execute(
        """
        INSERT INTO config_change (
            id, target_table, target_column, target_id, previous_value, secret, actor, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (new_id(), table, column, row_id, previous, secret, actor, moment, moment),
    )


def _column(table: str, column: str) -> Column:
    spec = TABLES.get(table)
    if spec is None or column not in spec:
        raise ConfigError(400, {"code": "column_not_configurable", "message": "this column is not configurable"})
    return spec[column]


def _row_key(table: str) -> str | None:
    if table in SINGLETONS:
        return None
    return ROW_KEY.get(table)


def _coerce(spec: Column, value: Any) -> Any:
    if value is None:
        if spec.nullable:
            return None
        raise ConfigError(400, {"code": "value_required", "message": "value is required"})
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ConfigError(400, {"code": "invalid_value", "message": "expected a boolean"})
        return value
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(400, {"code": "invalid_value", "message": "expected an integer"})
        if value < 0:
            raise ConfigError(400, {"code": "invalid_value", "message": "expected a positive integer"})
        return value
    if not isinstance(value, str):
        raise ConfigError(400, {"code": "invalid_value", "message": "expected a string"})
    if spec.choices and value not in spec.choices:
        raise ConfigError(400, {"code": "invalid_value", "message": "value is not one of the allowed choices"})
    return value


def _parse_previous(spec: Column, raw: str | None) -> Any:
    if raw is None:
        return None
    if spec.type == "bool":
        return raw == "true"
    if spec.type == "int":
        return int(raw)
    return raw


def _plain(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encrypt(value: Any) -> str | None:
    if value is None:
        return None
    return _cipher().encrypt(str(value).encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def _plain_field(table: str, column: str, value: Any) -> Any:
    spec = TABLES.get(table, {}).get(column)
    if spec is not None and spec.secret and value:
        return _decrypt(str(value))
    return value


def plain_singleton(table: str) -> dict[str, Any]:
    """Internal runtime read with decrypted secrets."""
    if table not in SINGLETONS:
        raise ConfigError(400, {"code": "not_singleton", "message": f"{table} is not a singleton table"})
    with _session() as conn:
        row = _find(conn, table, None)
    if row is None:
        raise ConfigError(503, {"code": "config_incomplete", "message": f"{table} is not seeded"})
    spec = TABLES[table]
    plain: dict[str, Any] = {}
    for key, value in row.items():
        if key in {"id", "created_at", "updated_at", "deleted_at"}:
            continue
        if key not in spec:
            continue
        plain[key] = _plain_field(table, key, value)
    return plain


def plain_provider(provider_id: str) -> dict[str, Any] | None:
    with _session() as conn:
        row = _find(conn, "model_provider", provider_id)
    if row is None:
        return None
    return {
        "base_url": row.get("base_url"),
        "model_name": row.get("model_name"),
        "api_key": _plain_field("model_provider", "api_key", row.get("api_key")),
    }


def plain_providers() -> dict[str, dict[str, Any]]:
    """Every model provider, with api_key decrypted. Empty when ops is not configured."""
    if not configured():
        return {}
    with _session() as conn:
        rows = conn.execute(
            """
            SELECT id, base_url, model_name, api_key
            FROM model_provider
            WHERE deleted_at IS NULL
            """
        ).fetchall()
    return {
        str(row["id"]): {
            "base_url": row.get("base_url"),
            "model_name": row.get("model_name"),
            "api_key": _plain_field("model_provider", "api_key", row.get("api_key")),
        }
        for row in rows
    }


def plain_models() -> list[dict[str, str]]:
    """Chat models the platform exposes. Empty when ops is not configured."""
    if not configured():
        return []
    with _session() as conn:
        rows = conn.execute(
            """
            SELECT id, provider_id, name, label
            FROM model
            WHERE deleted_at IS NULL
            ORDER BY id
            """
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "provider_id": str(row["provider_id"]),
            "name": str(row["name"]),
            "label": str(row["label"]),
        }
        for row in rows
    ]


def _cipher() -> Fernet:
    key = os.environ.get("VIKNOW_OPS_SECRET")
    if not key:
        raise ConfigError(503, {"code": "ops_secret_missing", "message": "VIKNOW_OPS_SECRET is not set"})
    try:
        return Fernet(key.encode())
    except (ValueError, InvalidToken) as exc:
        raise ConfigError(503, {"code": "ops_secret_invalid", "message": "VIKNOW_OPS_SECRET is not a Fernet key"}) from exc


def _mask_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    spec = TABLES.get(table, {})
    masked: dict[str, Any] = {}
    for key, value in row.items():
        column = spec.get(key)
        if column is not None and column.secret:
            masked[key] = {"set": bool(value)}
        elif hasattr(value, "isoformat"):
            masked[key] = stamp(value)
        else:
            masked[key] = value
    return masked


def _change_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "target_table": row["target_table"],
        "target_column": row["target_column"],
        "target_id": row["target_id"],
        "previous_value": None if row["secret"] else row["previous_value"],
        "secret": row["secret"],
        "actor": row["actor"],
        "created_at": stamp(row["created_at"]),
    }


_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS model_provider (
        id text PRIMARY KEY,
        base_url text NOT NULL,
        model_name text,
        api_key text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model (
        id text PRIMARY KEY,
        provider_id text NOT NULL REFERENCES model_provider (id),
        name text NOT NULL,
        label text NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_model (
        agent_id text PRIMARY KEY,
        model_id text NOT NULL REFERENCES model (id),
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS postgres_connection (
        id text PRIMARY KEY,
        host text NOT NULL,
        port integer NOT NULL,
        "user" text NOT NULL,
        password text,
        database text NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS redis_connection (
        id text PRIMARY KEY,
        host text NOT NULL,
        port integer NOT NULL,
        username text NOT NULL,
        database integer NOT NULL,
        password text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS neo4j_connection (
        id text PRIMARY KEY,
        uri text NOT NULL,
        username text NOT NULL,
        password text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS s3_client (
        id text PRIMARY KEY,
        endpoint text NOT NULL,
        public_endpoint text NOT NULL,
        bucket text NOT NULL,
        region text NOT NULL,
        url_expires_seconds integer NOT NULL,
        access_key text,
        secret_key text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS retrieval_setting (
        id text PRIMARY KEY,
        library text NOT NULL,
        mode text NOT NULL,
        top_k integer NOT NULL,
        rerank_enabled boolean NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_setting (
        id text PRIMARY KEY,
        segmentation text NOT NULL,
        asr_index text NOT NULL,
        asr_qa text NOT NULL,
        ocr text NOT NULL,
        mineru_model text NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observe_setting (
        id text PRIMARY KEY,
        langfuse_enabled boolean NOT NULL,
        langfuse_host text NOT NULL,
        langfuse_public_key text,
        langfuse_secret_key text,
        tracing_enabled boolean NOT NULL,
        media_enabled boolean NOT NULL,
        otel_enabled boolean NOT NULL,
        otel_endpoint text NOT NULL,
        usage_enabled boolean NOT NULL,
        usage_url text NOT NULL,
        usage_token text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_setting (
        id text PRIMARY KEY,
        log_level text NOT NULL,
        admin_username text NOT NULL,
        admin_password text,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config_change (
        id text PRIMARY KEY,
        target_table text NOT NULL,
        target_column text NOT NULL,
        target_id text,
        previous_value text,
        secret boolean NOT NULL,
        actor text NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        deleted_at timestamptz
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS postgres_connection_singleton ON postgres_connection ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS redis_connection_singleton ON redis_connection ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS neo4j_connection_singleton ON neo4j_connection ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS s3_client_singleton ON s3_client ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS retrieval_setting_singleton ON retrieval_setting ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS index_setting_singleton ON index_setting ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS observe_setting_singleton ON observe_setting ((1)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS service_setting_singleton ON service_setting ((1)) WHERE deleted_at IS NULL",
]
