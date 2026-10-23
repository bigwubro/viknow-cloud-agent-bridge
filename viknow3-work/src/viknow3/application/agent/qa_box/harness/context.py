"""Harness context bound for one deepagents turn."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from viknow3.application.agent.qa_box.harness.citations import CitationRegistry
from viknow3.application.agent.qa_box.harness.facts import HarnessRunFacts

EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class HarnessRunContext:
    facts: HarnessRunFacts
    primary_skill: str
    citations: CitationRegistry
    emit: EmitFn | None = None


_harness: ContextVar[HarnessRunContext | None] = ContextVar("viknow_harness_run", default=None)


def bind_harness(ctx: HarnessRunContext) -> object:
    return _harness.set(ctx)


def reset_harness(token: object) -> None:
    _harness.reset(token)


def current_harness() -> HarnessRunContext | None:
    return _harness.get()
