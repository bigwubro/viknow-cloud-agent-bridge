"""Minimal OpenTelemetry hooks for HyperRAG adapter (no-op unless wired later)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

ATTR_PHASE = "viknow.phase"
ATTR_RUN_ID = "viknow.run_id"


@contextmanager
def start_span_sync(_name: str, **_attrs: Any) -> Iterator[None]:
    yield


@asynccontextmanager
async def start_span(_name: str, **_attrs: Any) -> AsyncIterator[None]:
    yield
