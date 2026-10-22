"""Build the viknow-v3 graph (viknow-v2 profile parity)."""

from __future__ import annotations

from typing import Any

from viknow3.application.agent.knowledge_assistant.backends import build_filesystem_workspace_backend
from viknow3.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow3.application.agent.knowledge_assistant.llm import resolve_chat_model
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.session_workspace import workspace_upload_hint
from viknow3.application.agent.knowledge_assistant.tools import build_tools
from viknow3.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config
from viknow3.application.agent.knowledge_assistant.workspace_staging import workspace_skills_source


def build_graph(
    model_selector: str | None,
    checkpointer: Any,
    session_id: str,
    *,
    backend: Any | None = None,
) -> Any:
    """create_deep_agent; bubblewrap runs must pass the live sandbox backend."""
    from deepagents import create_deep_agent

    profile = load_viknow_v2_profile()
    cfg = load_deepagents_workspace_config()
    if backend is None:
        backend = build_filesystem_workspace_backend(session_id)
    system_prompt = runtime_instruction(profile, "deepagents") + workspace_upload_hint(session_id)
    skill_root = workspace_skills_source(cfg.backend)
    return create_deep_agent(
        model=resolve_chat_model(model_selector),
        tools=build_tools(profile),
        system_prompt=system_prompt,
        skills=[skill_root],
        backend=backend,
        checkpointer=checkpointer,
        name="deepagents",
    )
