"""The one Postgres server. Each business has its own database."""

from __future__ import annotations

import os
from typing import Any

ACCESS = "access"
AGENT = "agent"
HYPERRAG = "hyperrag"

_ENV = {
    ACCESS: "VIKNOW_ACCESS_DSN",
    AGENT: "VIKNOW_AGENT_DSN",
    HYPERRAG: "VIKNOW_HYPERRAG_DSN",
}


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


def connect(business: str) -> Any:
    import psycopg

    return psycopg.connect(dsn(business), autocommit=True)


def database_name(business: str) -> str:
    return dsn(business).rsplit("/", 1)[-1].split("?", 1)[0]


def require_test_database(business: str) -> None:
    name = database_name(business)
    if not name.endswith("_test"):
        raise RuntimeError(f"{business} database {name} is not a test database")
