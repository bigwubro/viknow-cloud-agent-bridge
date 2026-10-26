"""Stable I/O types between the viknow3 shell and DeepAgents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class AgentTurnInput:
    """What the shell passes into one DeepAgents turn."""

    session_id: str
    run_id: str
    user_id: str
    user_message: str
    library_ids: tuple[str, ...] = ()
    model_selector: str | None = None
    workspace_id: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()


@dataclass
class AgentTurnResult:
    """Terminal artifacts the shell persists on run_refs / SSE."""

    output: str = ""
    citations: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentRuntimeDeps:
    """Injected capabilities. The agent runtime does not import viknow3.access."""

    library_allowed: Callable[[str], bool]
    emit_event: Callable[[str, dict[str, Any]], None]
    hyperrag_factory: Callable[[], Any] | None = None
    actio_factory: Callable[[], Any] | None = None
