"""Build the viknow-v3 graph (viknow-v2 profile parity)."""

from __future__ import annotations

from typing import Any

from viknow3.application.agent.knowledge_assistant.backends import build_deepagents_backend, workspace_skills_source
from viknow3.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow3.application.agent.knowledge_assistant.llm import resolve_chat_model
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.session_workspace import materialize_session_uploads, workspace_upload_hint
from viknow3.application.agent.knowledge_assistant.tools import build_tools


def build_graph(model_selector: str | None, checkpointer: Any, session_id: str) -> Any:
    """create_deep_agent with the viknow-v2 prompt, tools, skills, and session workspace."""
    from deepagents import create_deep_agent

    profile = load_viknow_v2_profile()
    materialize_session_uploads(session_id)
    system_prompt = runtime_instruction(profile, "deepagents") + workspace_upload_hint(session_id)
    return create_deep_agent(
        model=resolve_chat_model(model_selector),
        tools=build_tools(profile),
        system_prompt=system_prompt,
        skills=[workspace_skills_source()],
        backend=build_deepagents_backend(session_id, profile.enabled_skills),
        checkpointer=checkpointer,
        name="deepagents",
    )
