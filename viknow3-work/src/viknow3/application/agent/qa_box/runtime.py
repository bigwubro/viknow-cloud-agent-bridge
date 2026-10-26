"""QA agent turn: delegates to agents.expert_buddy."""

from __future__ import annotations

from viknow3.application.agent.viknow2_adapter.vendor_runtime import ViknowVendorRuntime, get_vendor_runtime
from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime

# Backward-compatible name for tests and imports.
QaBoxRuntime = ViknowVendorRuntime


def get_runtime() -> Viknow2AgentRuntime:
    return get_vendor_runtime()
