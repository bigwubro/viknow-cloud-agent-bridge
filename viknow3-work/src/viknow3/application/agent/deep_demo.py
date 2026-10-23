"""One demo agent built with deepagents.

create_deep_agent compiles the graph and agentcore runs it. This demo does not
replace qa_expert.
"""

from __future__ import annotations

from typing import Any


def deepagents_demo_graph(checkpointer: Any) -> Any:
    from deepagents import create_deep_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    class _DemoModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "viknow-deepagents-demo"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
            text = f"deepagents：{_question(messages)}"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        def _stream(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any):
            yield ChatGenerationChunk(message=AIMessageChunk(content=f"deepagents：{_question(messages)}"))

    return create_deep_agent(
        model=_DemoModel(),
        system_prompt="这是 deepagents demo。用一句话回答用户。",
        checkpointer=checkpointer,
        name="deepagents-demo",
    )


def _question(messages: list) -> str:
    for message in reversed(messages):
        kind = getattr(message, "type", None) or getattr(message, "role", None)
        if kind not in ("human", "user"):
            continue
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else str(content)
    if not messages:
        return ""
    content = getattr(messages[-1], "content", "")
    return content if isinstance(content, str) else str(content)
