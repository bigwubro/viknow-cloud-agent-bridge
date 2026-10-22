"""Identifiers and timestamps shared by the access layer and the service clients."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def stamp(value: datetime) -> str:
    return value.isoformat()
