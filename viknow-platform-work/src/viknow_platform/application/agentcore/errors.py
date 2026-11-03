"""Errors raised inside Agent Service. The client re-exports them to the access layer."""

from __future__ import annotations

from typing import Any


class AgentServiceError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
