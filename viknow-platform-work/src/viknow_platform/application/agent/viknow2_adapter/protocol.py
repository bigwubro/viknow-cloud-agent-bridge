"""Protocol for a swappable viknow2 agent runtime."""

from __future__ import annotations

from typing import Protocol

from viknow_platform.application.agent.viknow2_adapter.types import AgentTurnInput, AgentTurnResult, AgentRuntimeDeps
from viknow_platform.application.agentcore.environment.context import RunState
from viknow_platform.application.agentcore.runtime.events import RunEvents


class Viknow2AgentRuntime(Protocol):
    """Black-box agent: graph, harness, tools, skills live behind this."""

    async def run_turn(
        self,
        turn: AgentTurnInput,
        state: RunState,
        events: RunEvents,
        deps: AgentRuntimeDeps,
    ) -> AgentTurnResult:
        """Execute one user turn (may stream via events.emit / deps.emit_event)."""
