"""Scheduling domain contracts: ExecUnit, Priority, slot requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Priority(StrEnum):
    """Execution unit priority tiers."""

    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    IDLE = "idle"

    @property
    def rank(self) -> int:
        if self is Priority.INTERACTIVE:
            return 0
        if self is Priority.BACKGROUND:
            return 1
        return 2

    @property
    def vllm_priority(self) -> int:
        """vLLM `/v1/chat/completions` `priority`: smaller value is scheduled first.

        Agent traffic uses ``INTERACTIVE`` (0). Indexing uses ``BACKGROUND`` (10).
        The field is inert unless the engine is started with
        ``--scheduling-policy priority``.
        """

        if self is Priority.INTERACTIVE:
            return 0
        if self is Priority.BACKGROUND:
            return 10
        return 20


def extra_body_with_priority(
    extra_body: dict[str, Any] | None,
    priority: Priority,
) -> dict[str, Any]:
    """Merge vLLM request priority into an OpenAI-compatible ``extra_body``."""

    body = dict(extra_body or {})
    body.setdefault("priority", priority.vllm_priority)
    return body


def private_vllm_priority(
    priority: Priority,
    *,
    provider_name: str | None = None,
    ref: str | None = None,
    model: str | None = None,
) -> int | None:
    """Return a vLLM priority only for the private / ``vllm/`` completion backend."""

    if (provider_name or "") == "private":
        return priority.vllm_priority
    if (ref or "").startswith("private:"):
        return priority.vllm_priority
    if (model or "").startswith("vllm/"):
        return priority.vllm_priority
    return None


class UnitKind(StrEnum):
    """Whether a scheduled unit is a tool call or model stream."""

    TOOL = "tool"
    MODEL = "model"


@dataclass(frozen=True)
class SlotRequest:
    """A resource slot request for streaming or long-lived work (e.g. chat model)."""

    source_id: str
    priority: Priority
    resources: tuple[str, ...]
    unit_kind: UnitKind = UnitKind.TOOL
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecUnit:
    """An execution unit submitted to ResourceScheduler for resource arbitration."""

    source_id: str
    priority: Priority
    resources: tuple[str, ...]
    handler: Callable[[], Awaitable[Any]]
    timeout: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    unit_kind: UnitKind = UnitKind.TOOL


class RunBudgetCancelledError(Exception):
    """Raised when a queued or in-flight unit is cancelled by run budget exhaustion."""

    def __init__(
        self,
        message: str = "",
        *,
        dimensions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.dimensions = dimensions


class ToolQueueTimeoutError(TimeoutError):
    """Raised when a tool never receives scheduler admission before its deadline."""
