"""Catalog database. Production uses Postgres; unit tests may use in-memory SQLite."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from viknow_storage.models import Base, CATALOG_SCHEMA


def make_engine(database_url: str) -> Engine:
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    with engine.begin() as conn:
        if database_url.startswith("sqlite"):
            conn.execute(text(f'ATTACH DATABASE ":memory:" AS "{CATALOG_SCHEMA}"'))
        else:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{CATALOG_SCHEMA}"'))
    Base.metadata.create_all(engine)
    return engine


def session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
