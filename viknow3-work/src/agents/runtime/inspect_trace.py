"""Optional debug trace capture for inspect extractor internals."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InspectTraceCollector:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, tool: str, stage: str, payload: dict[str, Any]) -> None:
        self.entries.append(
            {
                "tool": tool,
                "stage": stage,
                "payload": payload,
            }
        )


_ACTIVE_COLLECTOR: ContextVar[InspectTraceCollector | None] = ContextVar(
    "viknow_inspect_trace_collector",
    default=None,
)


def activate_inspect_trace_collector(collector: InspectTraceCollector) -> Token:
    return _ACTIVE_COLLECTOR.set(collector)


def reset_inspect_trace_collector(token: Token) -> None:
    _ACTIVE_COLLECTOR.reset(token)


def active_inspect_trace_collector() -> InspectTraceCollector | None:
    return _ACTIVE_COLLECTOR.get()


def record_inspect_stage(*, tool: str, stage: str, payload: dict[str, Any]) -> None:
    collector = active_inspect_trace_collector()
    if collector is None:
        return
    collector.record(tool=tool, stage=stage, payload=payload)
