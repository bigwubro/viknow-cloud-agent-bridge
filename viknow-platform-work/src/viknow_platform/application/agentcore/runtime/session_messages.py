"""Unified session transcript for deepagents (LangGraph) and ADK."""

from __future__ import annotations

import asyncio
from typing import Any

from viknow_platform.application.agentcore.registry.catalog import AGENT_ADK, AGENT_DEEPAGENTS
from viknow_platform.application.agentcore.runtime.langgraph_runner import thread_config
from viknow_platform.application.agentcore.runtime.loop import LOOP


async def _deepagents_messages(session_id: str) -> list[Any]:
    from viknow_platform.application.agent.knowledge_assistant.builder import build_graph
    from viknow_platform.application.agentcore.runtime.checkpoint import checkpointer

    saver = await checkpointer()
    graph = await asyncio.to_thread(build_graph, None, saver, session_id, backend=None)
    snapshot = await graph.aget_state(thread_config(session_id))
    return list((snapshot.values or {}).get("messages") or [])


async def _adk_messages(session_id: str) -> list[Any]:
    from viknow_platform.application.agent.knowledge_assistant.adk_session_messages import (
        _session_user_id,
        load_adk_session_messages,
    )

    user_id = _session_user_id(session_id)
    if not user_id:
        return []
    return await load_adk_session_messages(user_id, session_id)


async def session_messages_async(profile_id: str, session_id: str) -> list[Any]:
    if profile_id == AGENT_ADK:
        return await _adk_messages(session_id)
    if profile_id == AGENT_DEEPAGENTS:
        return await _deepagents_messages(session_id)
    return []


def session_messages(profile_id: str, session_id: str) -> list[Any]:
    """LangChain messages for a session (checkpoint or ADK events)."""
    return LOOP.call(session_messages_async(profile_id, session_id))
