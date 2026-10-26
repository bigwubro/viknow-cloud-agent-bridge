"""Graphs this package delivers to agentcore."""

from __future__ import annotations

from typing import Any

import os

from viknow3.application.agent.qa_expert.model import ScriptedModel
from viknow3.application.agent.qa_expert.tools import knowledge_search


def _script_model_enabled() -> bool:
    return os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() in {"1", "true", "yes"}


def _resolve_chat_model(model_selector: str | None) -> Any:
    from langchain_openai import ChatOpenAI

    from viknow3.ops.config.model_resolve import resolve_openai_chat

    explicit = model_selector or os.environ.get("VIKNOW_AGENT_CHAT_MODEL") or os.environ.get("OPENAI_MODEL")
    resolved = resolve_openai_chat(explicit or "server-default")
    max_input_tokens = int(os.environ.get("VIKNOW_CHAT_MAX_INPUT_TOKENS", "256000"))
    return ChatOpenAI(
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        temperature=resolved.temperature,
        stream_usage=True,
        profile={"max_input_tokens": max_input_tokens},
    )

MANUAL_QA_PROMPT = "只根据 knowledge_search 的结果回答，没有检索结果时说明查不到。"


def manual_qa_graph(model_selector: str | None, checkpointer: Any) -> Any:
    from langgraph.prebuilt import create_react_agent

    model = ScriptedModel() if _script_model_enabled() else _resolve_chat_model(model_selector)
    return create_react_agent(
        model,
        [knowledge_search],
        prompt=MANUAL_QA_PROMPT,
        checkpointer=checkpointer,
        name="manual-qa",
    )
