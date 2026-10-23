"""Context variables for passing per-run source identity to tool/model boundaries."""

from __future__ import annotations

from contextvars import ContextVar

#: The current run_id for scheduler source tracking.
#: Set by the runtime when a run starts; read by tool ainvoke wrappers
#: to construct ExecUnit.source_id.
current_source_id: ContextVar[str | None] = ContextVar("scheduler_source_id", default=None)


def set_source_id(source_id: str | None) -> None:
    """Set the current scheduler source_id context variable."""
    current_source_id.set(source_id)


def get_source_id() -> str:
    """Get the current source_id, falling back to 'unknown'."""
    return current_source_id.get() or "unknown"
