"""Deterministic chat model for MVP when no external LLM is configured."""

from __future__ import annotations

import json
from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


def _user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", None) in ("human", "user") or getattr(message, "role", None) == "user":
            content = getattr(message, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def _since_last_user(messages: list[BaseMessage]) -> list[BaseMessage]:
    for index in range(len(messages) - 1, -1, -1):
        if getattr(messages[index], "type", None) in ("human", "user"):
            return list(messages[index:])
    return list(messages)


def _tool_names(messages: list[BaseMessage]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
            name = getattr(message, "name", None)
            if name:
                names.add(str(name))
    return names


class KnowledgeAssistantModel(BaseChatModel):
    """Drives discover → refine → search → 答案 for mock MVP runs."""

    @property
    def _llm_type(self) -> str:
        return "viknow-knowledge-assistant-mvp"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _next_action(self, messages: list[BaseMessage]) -> AIMessage:
        question = _user_text(messages).strip()
        messages = _since_last_user(messages)
        done = _tool_names(messages)
        if "knowledge_discover" not in done:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-discover",
                        "name": "knowledge_discover",
                        "args": {"query": question or None},
                    }
                ],
            )
        library_id = ""
        for message in messages:
            if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
                try:
                    payload = json.loads(str(message.content))
                    if payload.get("error") == "permission_denied":
                        return AIMessage(
                            content=(
                                "当前会话没有可用的知识库权限。请在对话页先勾选一个知识库，"
                                "或新建对话后再试。"
                            )
                        )
                    libs = payload.get("libraries") or []
                    if libs:
                        library_id = str(libs[0].get("library_id") or library_id)
                except json.JSONDecodeError:
                    pass
        if not library_id:
            return AIMessage(content="未能发现可用知识库，请确认 library_ids 已授权。")
        if "query_refine" not in done:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-refine",
                        "name": "query_refine",
                        "args": {"query": question, "library_id": library_id},
                    }
                ],
            )
        if "knowledge_search" not in done:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-search",
                        "name": "knowledge_search",
                        "args": {"query": question, "library_id": library_id},
                    }
                ],
            )
        snippet = ""
        for message in reversed(messages):
            if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
                snippet = str(message.content)
                break
        answer = f"答案：根据当前库检索结果，{question} 的要点如下。\n\n{snippet[:600]}"
        return AIMessage(content=answer)

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        message = self._next_action(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        message = self._next_action(messages)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", tool_calls=list(message.tool_calls)),
            )
            return
        text = message.content if isinstance(message.content, str) else str(message.content)
        size = max(1, len(text) // 8)
        for index in range(0, len(text), size):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[index : index + size]))
