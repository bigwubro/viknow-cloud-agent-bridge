"""Build the viknow-v3 graph (viknow-v2 profile parity)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from viknow3.application.agent.knowledge_assistant.llm import resolve_chat_model
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.tools import build_tools

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


def build_graph(model_selector: str | None, checkpointer: Any) -> Any:
    """create_deep_agent with the viknow-v2 prompt, tools, and skills."""
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    profile = load_viknow_v2_profile()
    return create_deep_agent(
        model=resolve_chat_model(model_selector),
        tools=build_tools(profile),
        system_prompt=profile.system_prompt,
        skills=list(profile.enabled_skills),
        backend=FilesystemBackend(root_dir=str(_SKILLS_ROOT)),
        checkpointer=checkpointer,
        name="deepagents",
    )
