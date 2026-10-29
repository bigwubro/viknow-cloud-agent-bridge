"""DeepAgents-based 知识助手 (viknow-v3, viknow-v2 profile parity)."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "build_graph":
        from viknow_platform.application.agent.knowledge_assistant.builder import build_graph

        return build_graph
    raise AttributeError(name)


__all__ = ["build_graph"]
