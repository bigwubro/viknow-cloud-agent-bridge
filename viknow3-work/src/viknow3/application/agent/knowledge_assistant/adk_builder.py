"""Build an ADK LlmAgent from the shared viknow-v2 profile."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.environment._local_environment import LocalEnvironment
from google.adk.tools.skill_toolset import SkillToolset

from viknow3.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.tools import build_tools
from viknow3.application.agent.qa_expert.grant import current_user_message

_SKILLS_PACKAGE_ROOT = Path(__file__).resolve().parent / "skills"
_ADK_APP = "viknow3_adk"


def _adk_installed() -> bool:
    return importlib.util.find_spec("google.adk") is not None


def litellm_model(model_selector: str | None = None) -> str:
    """LiteLLM model id for ADK (OpenAI-compatible private endpoint)."""
    import os

    if os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() in {"1", "true", "yes"}:
        return "openai/viknow-script"

    from viknow3.ops.config.model_resolve import resolve_openai_chat

    resolved = resolve_openai_chat(model_selector or "server-default")
    os.environ["OPENAI_API_BASE"] = resolved.base_url.rstrip("/")
    os.environ["OPENAI_API_KEY"] = resolved.api_key
    return f"openai/{resolved.model}"


def _langchain_tool_to_adk(tool: Any) -> FunctionTool:
    def _invoke(*args: Any, **kwargs: Any) -> Any:
        import json

        payload: dict[str, Any]
        if args and isinstance(args[0], dict):
            payload = dict(args[0])
        elif args and isinstance(args[0], str):
            try:
                parsed = json.loads(args[0])
                payload = parsed if isinstance(parsed, dict) else {"query": args[0]}
            except json.JSONDecodeError:
                payload = {"query": args[0]}
        else:
            payload = dict(kwargs)
        nested = payload.get("input")
        if isinstance(nested, dict):
            payload = nested
        if tool.name == "query_refine" and not str(payload.get("query") or "").strip():
            hint = current_user_message()
            if hint:
                payload = {**payload, "query": hint}
        try:
            return tool.invoke(payload)
        except Exception as exc:
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                return json.dumps(
                    {"error": "invalid_tool_args", "tool": tool.name, "detail": exc.errors()},
                    ensure_ascii=False,
                )
            raise

    _invoke.__name__ = tool.name
    _invoke.__doc__ = tool.description or tool.name
    return FunctionTool(_invoke)


def _resolve_skills_folder(session_skills_dir: Path | None) -> Path:
    if session_skills_dir is not None and session_skills_dir.is_dir() and any(session_skills_dir.iterdir()):
        return session_skills_dir.resolve()
    return _SKILLS_PACKAGE_ROOT.resolve()


def build_llm_agent(
    model_selector: str | None = None,
    instruction: str | None = None,
    *,
    session_workspace_root: Path | None = None,
    session_skills_dir: Path | None = None,
) -> LlmAgent:
    if not _adk_installed():
        raise RuntimeError("google-adk is not installed")
    profile = load_viknow_v2_profile()
    profile_tools = [_langchain_tool_to_adk(item) for item in build_tools(profile)]
    skills_folder = _resolve_skills_folder(session_skills_dir)
    workdir = (session_workspace_root or skills_folder).resolve()
    environment = LocalEnvironment(working_dir=workdir)
    skill_toolset = SkillToolset(
        skills_folder=skills_folder,
        environment=environment,
        additional_tools=profile_tools,
    )
    return LlmAgent(
        name=_ADK_APP,
        model=litellm_model(model_selector),
        instruction=instruction or runtime_instruction(profile, "adk"),
        tools=[*profile_tools, skill_toolset],
    )


def agent_tool_names(agent: LlmAgent) -> set[str]:
    names: set[str] = set()
    for tool in agent.tools or []:
        provided = getattr(tool, "_provided_tools_by_name", None)
        if isinstance(provided, dict):
            names.update(str(key) for key in provided)
        for item in getattr(tool, "_tools", None) or ():
            name = getattr(item, "name", None) or getattr(item, "__name__", None)
            if name:
                names.add(str(name))
    return names
