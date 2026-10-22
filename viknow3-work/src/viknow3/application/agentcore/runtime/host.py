"""Agent host. Builds each profile's graph and runs a turn as a task on the agent loop.

Concrete agents live in application/agent. A graph is rebuilt per run so model
config changes apply at once. Session memory lives in the Postgres checkpointer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from viknow3.application.agent.attached import adk_base_url
from viknow3.application.agent.knowledge_assistant.builder import build_graph as deepagents_graph
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.events.sink import EventSink
from viknow3.application.agentcore.outbound.gateway import OutboundGateway
from viknow3.application.agentcore.registry.catalog import AGENT_ADK, Profile
from viknow3.application.agentcore.runtime.a2a import run_a2a
from viknow3.application.agentcore.runtime.checkpoint import checkpointer
from viknow3.application.agentcore.runtime.events import RunEvents
from viknow3.application.agentcore.runtime.langgraph_runner import run_graph, thread_config
from viknow3.application.agentcore.runtime.loop import LOOP

log = logging.getLogger(__name__)

GraphBuilder = Callable[[str | None, Any], Any]

GRAPHS: dict[str, GraphBuilder] = {
    "deepagents": deepagents_graph,
}


class _RunGrant:
    """Libraries granted on this run, as seen by the agents' search tools."""

    def __init__(self, state: RunState, outbound: OutboundGateway) -> None:
        self._state = state
        self._outbound = outbound

    def libraries(self) -> list[str]:
        ctx = self._state.ctx
        return [library_id for library_id in ctx.library_ids if self._outbound.permission(ctx.user_id, library_id, None)]

    def allowed(self) -> bool:
        return bool(self.libraries())


async def build(profile_id: str, model: str | None) -> Any:
    builder = GRAPHS[profile_id]
    saver = await checkpointer()
    return await asyncio.to_thread(builder, model, saver)


async def run(state: RunState, profile: Profile, sink: EventSink, outbound: OutboundGateway) -> None:
    """One turn. Runs on the agent loop; its context (and so the grant) is private to this task."""
    from viknow3.application.agent.qa_expert import bind_grant

    events = state.events or RunEvents(state, sink)
    state.aio_task = asyncio.current_task()
    bind_grant(_RunGrant(state, outbound))
    try:
        events.started()
        if profile.runtime == "adk":
            endpoint = adk_base_url()
            if not endpoint:
                events.failed(
                    "VIKNOW_ADK_BASE_URL is not set; configure the remote ADK A2A endpoint to run this agent",
                    code="adk_not_configured",
                )
                return
            await run_a2a(endpoint, events)
        elif profile.id in GRAPHS:
            await run_graph(await build(profile.id, state.ctx.model), events)
        else:
            events.failed("no runtime for this profile", code="unknown_profile")
    except asyncio.CancelledError:
        events.interrupted(state.output or "")
    except Exception as exc:
        log.exception("agent run %s failed", state.ctx.run_id)
        events.failed(f"{type(exc).__name__}: {exc}")
    finally:
        state.done.set()


def session_messages(profile_id: str, session_id: str) -> list[Any]:
    """Messages the checkpointer holds for one session."""

    async def read() -> list[Any]:
        graph = await build(profile_id, None)
        snapshot = await graph.aget_state(thread_config(session_id))
        return list((snapshot.values or {}).get("messages") or [])

    return LOOP.call(read())
