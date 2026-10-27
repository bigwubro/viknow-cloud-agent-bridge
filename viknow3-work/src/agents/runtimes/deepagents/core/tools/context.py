"""Per-run context for ViKnow tool handlers."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(slots=True)
class KnowledgeSearchBudget:
    max_calls: int = 3
    calls: int = 0

    def try_acquire(self) -> bool:
        if self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    def reset(self) -> None:
        self.calls = 0


@dataclass(frozen=True, slots=True)
class ToolRunContext:
    run_id: str
    user_id: str
    workspace_id: str
    session_id: str
    allowed_library_ids: frozenset[str] | None = None
    precompiled_knowledge_context: str = ""
    original_question: str = ""
    original_question_factory: Callable[[], str] | None = None
    remaining_seconds_factory: Callable[[], float | None] | None = None
    answer_slots_factory: Callable[[], list[dict[str, Any]]] | None = None
    tool_gate_context_factory: Callable[[], Any] | None = None
    answer_plan_handler: Callable[[dict[str, Any]], str] | None = None
    segment_catalogs: dict[str, Any] = field(default_factory=dict)
    oracle_scope: dict[str, Any] | None = None
    oracle_task: dict[str, Any] | None = None
    knowledge_search_budget: KnowledgeSearchBudget = field(default_factory=KnowledgeSearchBudget)


_CURRENT_TOOL_RUN_CONTEXT: ContextVar[ToolRunContext | None] = ContextVar(
    "viknow_tool_run_context",
    default=None,
)


def set_tool_run_context(context: ToolRunContext | None) -> Token[ToolRunContext | None]:
    return _CURRENT_TOOL_RUN_CONTEXT.set(context)


def reset_tool_run_context(token: Token[ToolRunContext | None]) -> None:
    _CURRENT_TOOL_RUN_CONTEXT.reset(token)


def get_tool_run_context() -> ToolRunContext | None:
    return _CURRENT_TOOL_RUN_CONTEXT.get()


def get_allowed_library_ids() -> frozenset[str] | None:
    context = get_tool_run_context()
    return context.allowed_library_ids if context is not None else None


def is_library_allowed(library_id: str) -> bool:
    allowed = get_allowed_library_ids()
    return allowed is None or library_id in allowed


def acquire_knowledge_search_budget() -> KnowledgeSearchBudget | None:
    context = get_tool_run_context()
    if context is None:
        return None
    budget = context.knowledge_search_budget
    if not budget.try_acquire():
        return budget
    return None


def reset_knowledge_search_budget() -> None:
    context = get_tool_run_context()
    if context is not None:
        context.knowledge_search_budget.reset()


_QUESTION_TOOLS = frozenset(
    {
        "text_inspect",
        "image_inspect",
        "document_inspect",
        "video_inspect",
        "video_inspect_2_0",
    }
)


def with_run_question(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Attach runtime authority after schema validation; direct calls stay standalone.

    Focused questions select evidence, not a replacement task. Candidate identity
    and media sampling arguments are deliberately left untouched.
    """
    context = get_tool_run_context()
    if context is None:
        return arguments
    original_question = (
        context.original_question_factory()
        if context.original_question_factory
        else context.original_question
    )
    if not original_question:
        return arguments
    if tool_name == "query_refine":
        return {**arguments, "query": original_question}
    if tool_name not in _QUESTION_TOOLS:
        return arguments
    slots = context.answer_slots_factory() if context.answer_slots_factory else []
    task = {
        "original_question": original_question,
        "answer_slots": slots,
        "focused_question": str(arguments.get("question") or ""),
    }
    question = (
        "Verify the focused question against the immutable original task below. "
        "Preserve its subject, locating conditions, requested attributes, temporal relations "
        "and required modalities. Report which conditions this window cannot verify; "
        "do not narrow the original task to the focused question.\n"
        + json.dumps(task, ensure_ascii=False)
    )
    return {**arguments, "question": question}


def remaining_run_seconds() -> float | None:
    """One deadline shared by tools, internal refinements and answer review."""
    context = get_tool_run_context()
    if context is None or context.remaining_seconds_factory is None:
        return None
    remaining = context.remaining_seconds_factory()
    if remaining is not None and remaining <= 0:
        from agents.runtimes.deepagents.core.scheduler.domain import RunBudgetCancelledError

        raise RunBudgetCancelledError("original run budget exhausted")
    return remaining


@contextmanager
def scoped_model_deadline(timeout_seconds: float):
    """A copied thread context cannot outlive its caller's local review deadline."""
    context = get_tool_run_context()
    if context is None:
        yield
        return
    cancelled = threading.Event()
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        if cancelled.is_set():
            return 0.0
        local = deadline - time.monotonic()
        outer = context.remaining_seconds_factory() if context.remaining_seconds_factory else None
        return min(local, outer) if outer is not None else local

    token = set_tool_run_context(replace(context, remaining_seconds_factory=remaining))
    try:
        yield
    finally:
        cancelled.set()
        reset_tool_run_context(token)
