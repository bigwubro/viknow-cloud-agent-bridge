"""Shared session event persistence policy."""

from __future__ import annotations

from viknow.core.runtime.domain import AgentEventType

EPHEMERAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.MESSAGE_DELTA,
        AgentEventType.REASONING_DELTA,
    }
)

TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_INTERRUPTED,
    }
)
