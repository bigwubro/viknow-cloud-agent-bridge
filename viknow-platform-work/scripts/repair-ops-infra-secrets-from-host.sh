#!/usr/bin/env bash
# Re-encrypt postgres/redis/neo4j passwords in viknow_ops from host env (236 Playground bootstrap).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src:${ROOT}"
PYTHON="${VIKNOW_PYTHON:-${HOME}/viknow3/venv312/bin/python}"
[[ -x "$PYTHON" ]] || PYTHON="${HOME}/viknow-platform/venv312/bin/python"

OPS_ENV="${VIKNOW_OPS_ENV:-${HOME}/.viknow3/ops.env}"
DEPLOY_ENV="${VIKNOW_DEPLOY_ENV:-${HOME}/deploy.env}"
PG_DEMO="${VIKNOW_PG_ENV:-${HOME}/viknow-platform/pg-demo.env}"

"$PYTHON" - <<'PY'
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import psycopg

from viknow_platform.ops.config.store import _encrypt, plain_singleton


def load_env(path: str) -> None:
    if not path or not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


ops_env = os.environ.get("VIKNOW_OPS_ENV", os.path.expanduser("~/.viknow3/ops.env"))
deploy_env = os.environ.get("VIKNOW_DEPLOY_ENV", os.path.expanduser("~/deploy.env"))
pg_demo = os.environ.get("VIKNOW_PG_ENV", os.path.expanduser("~/viknow-platform/pg-demo.env"))

load_env(ops_env)
load_env(deploy_env)
load_env(pg_demo)

dsn = os.environ.get("VIKNOW_OPS_DSN")
if not dsn:
    raise SystemExit("VIKNOW_OPS_DSN missing (set in ops.env)")

hr = os.environ.get("VIKNOW_HYPERRAG_DSN", "")
if not hr:
    text = open(pg_demo, encoding="utf-8").read() if os.path.isfile(pg_demo) else ""
    m = re.search(r"^VIKNOW_HYPERRAG_DSN=(.+)$", text, re.M)
    hr = m.group(1).strip() if m else ""
if not hr:
    raise SystemExit("VIKNOW_HYPERRAG_DSN missing")

pg = urlparse(hr)
redis_pw = os.environ.get("REDIS_PASSWORD", "")
neo_pw = os.environ.get("VIKNOW_NEO4J_PASSWORD", "").strip() or os.environ.get("NEO4J_PASSWORD", "")

with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE postgres_connection SET host=%s, port=%s, "user"=%s, password=%s, database=%s, updated_at=now()
               WHERE deleted_at IS NULL""",
            (
                pg.hostname or "127.0.0.1",
                pg.port or 16432,
                pg.username,
                _encrypt(pg.password),
                (pg.path or "/viknow3_knowledge").lstrip("/"),
            ),
        )
        if redis_pw:
            cur.execute(
                """UPDATE redis_connection SET host=%s, port=%s, password=%s, updated_at=now()
                   WHERE deleted_at IS NULL""",
                ("127.0.0.1", int(os.environ.get("VIKNOW_REDIS_PORT", "16379")), _encrypt(redis_pw)),
            )
        if neo_pw:
            cur.execute(
                """UPDATE neo4j_connection SET password=%s, uri=%s, username=%s, updated_at=now()
                   WHERE deleted_at IS NULL""",
                (_encrypt(neo_pw), "neo4j://127.0.0.1:17487", "neo4j"),
            )
    conn.commit()

for table in ("postgres_connection", "redis_connection", "neo4j_connection"):
    row = plain_singleton(table)
    print(f"ok {table} secrets={'password' in row and bool(row.get('password'))}")
PY
