"""Tool domain contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    description: str
    async_handler: Callable[[dict[str, Any]], Awaitable[str]] | None = None
    execution_timeout_seconds: float | None = None
    resource_class: str | None = None
