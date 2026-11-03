"""ADK session rows live in the agent database under schema ``adk``."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from viknow_platform.pg import AGENT, connect, dsn

log = logging.getLogger(__name__)

_ADK_APP = "viknow_platform_adk"
_SCHEMA = "adk"


def _bind_adk_schema() -> None:
    from google.adk.sessions.schemas import v1 as schema_v1

    for table in schema_v1.Base.metadata.tables.values():
        table.schema = _SCHEMA


def _ensure_schema() -> None:
    with connect(AGENT) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")


@lru_cache(maxsize=1)
def _session_service() -> Any:
    _ensure_schema()
    _bind_adk_schema()
    from google.adk.sessions.database_session_service import DatabaseSessionService

    url = dsn(AGENT)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return DatabaseSessionService(db_url=url)


async def ensure_session(user_id: str, session_id: str) -> None:
    service = _session_service()
    existing = await service.get_session(app_name=_ADK_APP, user_id=user_id, session_id=session_id)
    if existing is not None:
        return
    await service.create_session(app_name=_ADK_APP, user_id=user_id, session_id=session_id)


async def delete_session(user_id: str, session_id: str) -> None:
    service = _session_service()
    try:
        await service.delete_session(app_name=_ADK_APP, user_id=user_id, session_id=session_id)
    except Exception as exc:
        log.debug("adk session delete skipped: %s", exc)
