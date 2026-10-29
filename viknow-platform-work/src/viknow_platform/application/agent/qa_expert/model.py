"""Offline model for manual-qa, used only when VIKNOW_USE_SCRIPT_MODEL is set (tests)."""

from __future__ import annotations

from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from viknow_platform.application.agent.qa_expert.pipeline import answer, rewrite


def _current_turn(messages: list[BaseMessage]) -> tuple[str, str | None]:
    question = ""
    passage: str | None = None
    for message in messages:
        content = message.content if isinstance(message.content, str) else str(message.content)
        if message.type == "human":
            question = rewrite(content)
            passage = None
        elif message.type == "tool":
            passage = content
    return question, passage


class ScriptedModel(BaseChatModel):
    """Asks for knowledge_search once per turn, then answers from the passage."""

    @property
    def _llm_type(self) -> str:
        return "viknow-manual-qa-script"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        question, passage = _current_turn(messages)
        if passage is None:
            return AIMessage(
                content="",
                tool_calls=[{"id": "call-search", "name": "knowledge_search", "args": {"query": question}}],
            )
        return AIMessage(content=answer(question, passage))

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        message = self._next(messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=list(message.tool_calls))
        )
