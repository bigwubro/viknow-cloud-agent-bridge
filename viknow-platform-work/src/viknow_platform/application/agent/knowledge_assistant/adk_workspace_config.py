"""ADK workspace / script sandbox settings."""

from __future__ import annotations

from dataclasses import dataclass
from viknow_platform.application.agent.knowledge_assistant.workspace_config import (
    DeepagentsSandboxConfig,
    _load_app_config,
    load_deepagents_workspace_config,
)


@dataclass(frozen=True)
class AdkWorkspaceConfig:
    script_sandbox: str = "host"
    sandbox: DeepagentsSandboxConfig = DeepagentsSandboxConfig()


def load_adk_workspace_config() -> AdkWorkspaceConfig:
    agent = _load_app_config().get("agent")
    if not isinstance(agent, dict):
        return AdkWorkspaceConfig(sandbox=load_deepagents_workspace_config().sandbox)
    adk = agent.get("adk")
    if not isinstance(adk, dict):
        return AdkWorkspaceConfig(sandbox=load_deepagents_workspace_config().sandbox)
    ws = adk.get("workspace")
    if not isinstance(ws, dict):
        return AdkWorkspaceConfig(sandbox=load_deepagents_workspace_config().sandbox)
    mode = str(ws.get("script_sandbox") or "host").strip().lower()
    sandbox = load_deepagents_workspace_config().sandbox
    return AdkWorkspaceConfig(script_sandbox=mode, sandbox=sandbox)
