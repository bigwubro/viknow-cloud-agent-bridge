"""Session transcript for the QA agent (LangGraph checkpoint)."""

from __future__ import annotations

from typing import Any

from viknow3.application.agentcore.registry.catalog import AGENT_DEEPAGENTS
from viknow3.application.agentcore.runtime.langgraph_runner import thread_config
from viknow3.application.agentcore.runtime.loop import LOOP


async def _deepagents_messages(session_id: str) -> list[Any]:
    from viknow3.application.agentcore.runtime.checkpoint import checkpointer

    saver = await checkpointer()
    config = thread_config(session_id)
    tup = await saver.aget_tuple(config)
    if tup is None:
        return []
    checkpoint = tup.checkpoint if hasattr(tup, "checkpoint") else tup[0]
    if not isinstance(checkpoint, dict):
        return []
    values = checkpoint.get("channel_values") or checkpoint.get("values") or {}
    return list(values.get("messages") or [])


async def session_messages_async(profile_id: str, session_id: str) -> list[Any]:
    if profile_id == AGENT_DEEPAGENTS:
        return await _deepagents_messages(session_id)
    return []


def session_messages(profile_id: str, session_id: str) -> list[Any]:
    """LangChain messages for a session (checkpoint)."""
    return LOOP.call(session_messages_async(profile_id, session_id))
