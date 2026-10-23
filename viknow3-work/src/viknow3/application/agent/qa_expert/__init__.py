"""QA expert agents registered by agentcore."""

from __future__ import annotations

from typing import Any

from viknow3.application.agent.qa_expert.grant import SearchGrant, bind_grant, reset_grant


def __getattr__(name: str) -> Any:
    if name == "manual_qa_graph":
        from viknow3.application.agent.qa_expert import agents

        return agents.manual_qa_graph
    raise AttributeError(name)


__all__ = ["SearchGrant", "bind_grant", "manual_qa_graph", "reset_grant"]
