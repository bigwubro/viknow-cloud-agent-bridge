"""Adapter boundary: viknow3 shell to the shared agent runtime."""

from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult


def get_runtime() -> Viknow2AgentRuntime:
    from viknow3.application.agent.viknow2_adapter.vendor_runtime import get_vendor_runtime

    return get_vendor_runtime()


__all__ = [
    "AgentRuntimeDeps",
    "AgentTurnInput",
    "AgentTurnResult",
    "Viknow2AgentRuntime",
    "get_runtime",
]
