"""Offline chat model used when VIKNOW_USE_SCRIPT_MODEL is set.

The shell injects this through the runtime's model factory. It is not a
checkpoint and it does not choose how conversation state is stored.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_SKILL_PATH = "/workspace/.skills/knowledge-qa-v2/SKILL.md"


def script_model_enabled() -> bool:
    return os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() in {"1", "true", "yes"}


def script_chat_model(_model_config: Any = None) -> "KnowledgeAssistantModel":
    # Summarization middleware reads this absolute limit. A fractional window
    # without it refuses to build the agent.
    return KnowledgeAssistantModel(profile={"max_input_tokens": 256000})


def _content(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _is_user(message: BaseMessage) -> bool:
    kind = getattr(message, "type", None)
    if kind not in ("human", "user") and getattr(message, "role", None) != "user":
        return False
    text = _content(message).lstrip()
    return not text.startswith("<runtime_feedback>") and not text.startswith("<qa_answer_plan>")


def _question(text: str) -> str:
    marker = "user_query:\n"
    if marker in text:
        return text.split(marker, 1)[1].split("\n", 1)[0].strip()
    return text.strip()


def _user_turns(messages: list[BaseMessage]) -> list[str]:
    return [_question(_content(message)) for message in messages if _is_user(message)]


def _since_last_user(messages: list[BaseMessage]) -> list[BaseMessage]:
    for index in range(len(messages) - 1, -1, -1):
        if _is_user(messages[index]):
            return list(messages[index:])
    return list(messages)


def _libraries(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    libraries = payload.get("libraries")
    if isinstance(libraries, list):
        return libraries
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("libraries"), list):
        return data["libraries"]
    return []


def _tool_names(messages: list[BaseMessage]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
            name = getattr(message, "name", None)
            if name:
                names.add(str(name))
    return names


class KnowledgeAssistantModel(BaseChatModel):
    """Drives read_file, discover, refine, search, then a fixed answer."""

    @property
    def _llm_type(self) -> str:
        return "viknow-knowledge-assistant-script"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        del tools, kwargs
        return self

    def _next_action(self, messages: list[BaseMessage]) -> AIMessage:
        turns = _user_turns(messages)
        question = turns[-1] if turns else ""
        prior = turns[:-1]
        current = _since_last_user(messages)
        done = _tool_names(current)
        if "read_file" not in done:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-read-skill",
                        "name": "read_file",
                        "args": {"file_path": _SKILL_PATH},
                    }
                ],
            )
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
        for message in current:
            if not (isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool"):
                continue
            try:
                payload = json.loads(_content(message))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("error") == "permission_denied":
                return AIMessage(content="当前会话没有可用的知识库权限。请在对话页先勾选一个知识库，或新建对话后再试。")
            libraries = _libraries(payload)
            if not libraries:
                continue
            first = libraries[0]
            if isinstance(first, dict):
                library_id = str(first.get("library_id") or first.get("id") or library_id)
        if not library_id:
            return AIMessage(content="未能发现可用知识库，请确认 library_ids 已授权。")
        if "query_refine" not in done:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-refine",
                        "name": "query_refine",
                        "args": {"query": question},
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
        for message in reversed(current):
            if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
                snippet = _content(message)
                break
        earlier = f"\n上文：{' | '.join(prior)}" if prior else ""
        answer = f"答案：根据当前库检索结果，{question} 的要点如下。{earlier}\n\n{snippet[:600]}"
        return AIMessage(content=answer)

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        del stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=self._next_action(messages))])

    def _stream(
        self,
        messages: list,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del stop, run_manager, kwargs
        message = self._next_action(messages)
        if message.tool_calls:
            chunks = [
                {
                    "name": call["name"],
                    "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    "id": call["id"],
                    "index": index,
                    "type": "tool_call_chunk",
                }
                for index, call in enumerate(message.tool_calls)
            ]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=chunks))
            return
        text = message.content if isinstance(message.content, str) else str(message.content)
        size = max(1, len(text) // 8)
        for index in range(0, len(text), size):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[index : index + size]))
