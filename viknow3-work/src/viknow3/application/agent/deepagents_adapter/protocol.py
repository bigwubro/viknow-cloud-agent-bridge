"""One DeepAgents turn, as the shell sees it."""

from __future__ import annotations

from typing import Protocol

from viknow3.application.agent.deepagents_adapter.types import AgentTurnInput, AgentTurnResult, AgentRuntimeDeps
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.events import RunEvents


class TurnRuntime(Protocol):
    """DeepAgents graph, tools, and skills stay behind this."""

    async def run_turn(
        self,
        turn: AgentTurnInput,
        state: RunState,
        events: RunEvents,
        deps: AgentRuntimeDeps,
    ) -> AgentTurnResult:
        """Execute one user turn (may stream via events.emit / deps.emit_event)."""
