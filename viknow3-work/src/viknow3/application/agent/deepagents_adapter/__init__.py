"""Shell adapter for the shared DeepAgents runtime."""

from viknow3.application.agent.deepagents_adapter.protocol import TurnRuntime
from viknow3.application.agent.deepagents_adapter.types import AgentTurnInput, AgentTurnResult


def get_runtime() -> TurnRuntime:
    from viknow3.application.agent.deepagents_adapter.vendor_runtime import get_adapter_runtime

    return get_adapter_runtime()


__all__ = [
    "AgentTurnInput",
    "AgentTurnResult",
    "TurnRuntime",
    "get_runtime",
]
