"""Adapter boundary: viknow3 shell ↔ viknow2 agent core."""

from viknow3.application.agent.viknow2_adapter.compat_runtime import Viknow2CompatRuntime, get_runtime
from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult

__all__ = [
    "AgentRuntimeDeps",
    "AgentTurnInput",
    "AgentTurnResult",
    "Viknow2AgentRuntime",
    "Viknow2CompatRuntime",
    "get_runtime",
]
