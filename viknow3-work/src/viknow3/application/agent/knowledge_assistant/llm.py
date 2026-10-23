"""Resolve chat model for viknow-v2 parity agent (real Qwen on 236, script model in tests only)."""

from __future__ import annotations

import os
from typing import Any


def script_model_enabled() -> bool:
    return os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() in {"1", "true", "yes"}


def resolve_chat_model(model_selector: str | None = None) -> Any:
    if script_model_enabled():
        from viknow3.application.agent.knowledge_assistant.model import KnowledgeAssistantModel

        return KnowledgeAssistantModel()

    from viknow3.ops.config.model_resolve import resolve_openai_chat

    explicit = (
        model_selector
        or os.environ.get("VIKNOW_AGENT_CHAT_MODEL")
        or os.environ.get("OPENAI_MODEL")
    )
    resolved = resolve_openai_chat(explicit or "server-default")

    from langchain_openai import ChatOpenAI

    # deepagents SummarizationMiddleware uses fractional limits; needs max_input_tokens profile.
    max_input_tokens = int(os.environ.get("VIKNOW_CHAT_MAX_INPUT_TOKENS", "256000"))
    return ChatOpenAI(
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        temperature=resolved.temperature,
        stream_usage=True,
        profile={"max_input_tokens": max_input_tokens},
    )
