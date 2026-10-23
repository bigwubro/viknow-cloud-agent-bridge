"""Session transcript for the QA agent (LangGraph checkpoint)."""

from __future__ import annotations

import asyncio
from typing import Any

from viknow3.application.agentcore.registry.catalog import AGENT_DEEPAGENTS
from viknow3.application.agentcore.runtime.langgraph_runner import thread_config
from viknow3.application.agentcore.runtime.loop import LOOP


async def _deepagents_messages(session_id: str) -> list[Any]:
    from viknow3.application.agent.qa_box.builder import build_graph
    from viknow3.application.agentcore.runtime.checkpoint import checkpointer

    saver = await checkpointer()
    graph = await asyncio.to_thread(build_graph, None, saver, session_id, backend=None)
    snapshot = await graph.aget_state(thread_config(session_id))
    return list((snapshot.values or {}).get("messages") or [])


async def session_messages_async(profile_id: str, session_id: str) -> list[Any]:
    if profile_id == AGENT_DEEPAGENTS:
        return await _deepagents_messages(session_id)
    return []


def session_messages(profile_id: str, session_id: str) -> list[Any]:
    """LangChain messages for a session (checkpoint)."""
    return LOOP.call(session_messages_async(profile_id, session_id))
