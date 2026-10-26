"""Wiring that builds one DeepAgentsRuntime per profile."""

from viknow3.application.agent.deepagents_adapter.vendor.factory import get_deepagents_runtime

__all__ = ["get_deepagents_runtime"]
