"""Adapter boundary: viknow3 shell ↔ QA black box."""

from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult


def get_runtime() -> Viknow2AgentRuntime:
    from viknow3.application.agent.qa_box.runtime import get_runtime as _get_runtime

    return _get_runtime()


def __getattr__(name: str):
    if name == "QaBoxRuntime":
        from viknow3.application.agent.qa_box.runtime import QaBoxRuntime

        return QaBoxRuntime
    raise AttributeError(name)


__all__ = [
    "AgentRuntimeDeps",
    "AgentTurnInput",
    "AgentTurnResult",
    "QaBoxRuntime",
    "Viknow2AgentRuntime",
    "get_runtime",
]
