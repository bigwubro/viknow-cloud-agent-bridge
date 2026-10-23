"""Graphs this package delivers to agentcore."""

from __future__ import annotations

from typing import Any

from viknow3.application.agent.qa_box.llm import resolve_chat_model, script_model_enabled
from viknow3.application.agent.qa_expert.model import ScriptedModel
from viknow3.application.agent.qa_expert.tools import knowledge_search

MANUAL_QA_PROMPT = "只根据 knowledge_search 的结果回答，没有检索结果时说明查不到。"


def manual_qa_graph(model_selector: str | None, checkpointer: Any) -> Any:
    from langgraph.prebuilt import create_react_agent

    model = ScriptedModel() if script_model_enabled() else resolve_chat_model(model_selector)
    return create_react_agent(
        model,
        [knowledge_search],
        prompt=MANUAL_QA_PROMPT,
        checkpointer=checkpointer,
        name="manual-qa",
    )
