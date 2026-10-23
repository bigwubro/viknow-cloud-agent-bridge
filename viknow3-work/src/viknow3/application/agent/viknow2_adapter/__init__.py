"""Adapter boundary: viknow3 shell ↔ QA black box."""

from viknow3.application.agent.qa_box.runtime import QaBoxRuntime, get_runtime
from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult

__all__ = [
    "AgentRuntimeDeps",
    "AgentTurnInput",
    "AgentTurnResult",
    "QaBoxRuntime",
    "Viknow2AgentRuntime",
    "get_runtime",
]
