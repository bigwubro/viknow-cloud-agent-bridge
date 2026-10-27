"""Turn an injected Postgres DSN into the async URL ADK's session service accepts."""

from __future__ import annotations


def async_postgres_url(dsn: str) -> str:
    text = dsn.strip()
    if text.startswith("postgres://"):
        text = "postgresql://" + text[len("postgres://") :]
    if text.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + text[len("postgresql+psycopg2://") :]
    if text.startswith("postgresql+asyncpg://") or text.startswith("postgresql+psycopg://"):
        return text
    if text.startswith("postgresql://"):
        return "postgresql+psycopg://" + text[len("postgresql://") :]
    return text
