"""Load ADK session events as LangChain messages (parity with LangGraph checkpointer)."""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from viknow_platform.application.agent.knowledge_assistant.adk_session import _ADK_APP, _session_service

log = logging.getLogger(__name__)


def _session_user_id(session_id: str) -> str | None:
    from viknow_platform.store import STORE

    for session in STORE.sessions.values():
        if not session.deleted and session.id == session_id:
            return session.user_id
    return None


def events_to_langchain_messages(events: list[Any]) -> list[BaseMessage]:
    """Best-effort mapping from ADK session events to LangChain message list."""
    messages: list[BaseMessage] = []
    for event in events:
        content = getattr(event, "content", None)
        if content is None:
            continue
        parts = list(getattr(content, "parts", None) or [])
        if not parts:
            continue
        role = str(getattr(content, "role", "") or "").lower()
        author = str(getattr(event, "author", "") or "").lower()
        is_user = author == "user" or role == "user"

        text_chunks: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_messages: list[ToolMessage] = []

        for part in parts:
            text = getattr(part, "text", None)
            if text:
                text_chunks.append(str(text))
            call = getattr(part, "function_call", None)
            if call is not None:
                name = str(getattr(call, "name", "") or "")
                args = getattr(call, "args", None) or {}
                call_id = str(getattr(call, "id", "") or name or "call")
                tool_calls.append({"id": call_id, "name": name, "args": dict(args) if isinstance(args, dict) else {}})
            response = getattr(part, "function_response", None)
            if response is not None:
                name = str(getattr(response, "name", "") or "")
                call_id = str(getattr(response, "id", "") or name or "call")
                payload = getattr(response, "response", None)
                if isinstance(payload, dict):
                    body = json.dumps(payload, ensure_ascii=False)
                else:
                    body = str(payload or "")
                tool_messages.append(ToolMessage(content=body, tool_call_id=call_id, name=name or None))

        if is_user:
            if text_chunks:
                messages.append(HumanMessage(content="".join(text_chunks)))
            continue

        if tool_calls:
            messages.append(AIMessage(content="".join(text_chunks), tool_calls=tool_calls))
        for tool_message in tool_messages:
            messages.append(tool_message)
        if text_chunks and not tool_calls and not tool_messages:
            messages.append(AIMessage(content="".join(text_chunks)))
    return messages


async def load_adk_session_messages(user_id: str, session_id: str) -> list[BaseMessage]:
    service = _session_service()
    session = await service.get_session(app_name=_ADK_APP, user_id=user_id, session_id=session_id)
    if session is None:
        return []
    return events_to_langchain_messages(list(session.events or []))


def load_adk_session_messages_sync(session_id: str) -> list[BaseMessage]:
    user_id = _session_user_id(session_id)
    if not user_id:
        return []

    async def _read() -> list[BaseMessage]:
        return await load_adk_session_messages(user_id, session_id)

    from viknow_platform.application.agentcore.runtime.loop import LOOP

    return LOOP.call(_read())
