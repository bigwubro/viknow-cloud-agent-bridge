"""Stable I/O types between viknow-platform access layer and viknow2 agent core."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class AgentTurnInput:
    """What the outer shell passes into the agent black box each run."""

    session_id: str
    run_id: str
    user_id: str
    user_message: str
    library_ids: tuple[str, ...] = ()
    model_selector: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()


@dataclass
class AgentTurnResult:
    """Terminal artifacts the shell persists on run_refs / SSE."""

    output: str = ""
    citations: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentRuntimeDeps:
    """Injected capabilities; viknow2 core must not import viknow_platform.access."""

    library_allowed: Callable[[str], bool]
    emit_event: Callable[[str, dict[str, Any]], None]
    hyperrag_factory: Callable[[], Any] | None = None
    actio_factory: Callable[[], Any] | None = None
