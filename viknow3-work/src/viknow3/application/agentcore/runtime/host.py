"""Agent host. One turn runs on the agent loop.

The profile id selects a package under src/agents. Libraries this run may
search are the ones the caller asked for and this user may access. Session
memory lives in the Postgres checkpointer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agents.layout import package_for_public_id
from viknow3.application.agent.deepagents_adapter import AgentRuntimeDeps, AgentTurnInput, get_runtime
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.events.sink import EventSink
from viknow3.application.agentcore.outbound.gateway import OutboundGateway
from viknow3.application.agentcore.registry.catalog import Profile
from viknow3.application.agentcore.runtime.events import RunEvents

log = logging.getLogger(__name__)


class _RunGrant:
    """Libraries granted on this run."""

    def __init__(self, state: RunState, outbound: OutboundGateway) -> None:
        self._state = state
        self._outbound = outbound

    def libraries(self) -> list[str]:
        ctx = self._state.ctx
        return [library_id for library_id in ctx.library_ids if self._outbound.permission(ctx.user_id, library_id, None)]


async def run(state: RunState, profile: Profile, sink: EventSink, outbound: OutboundGateway) -> None:
    """One turn. Runs on the agent loop."""
    events = state.events or RunEvents(state, sink)
    state.aio_task = asyncio.current_task()
    try:
        events.started()
        if package_for_public_id(profile.id) is None:
            events.failed("no runtime for this profile", code="unknown_profile")
            return
        libraries = tuple(_RunGrant(state, outbound).libraries())
        turn = AgentTurnInput(
            session_id=state.ctx.session_id,
            run_id=state.ctx.run_id,
            user_id=state.ctx.user_id,
            user_message=state.ctx.message,
            library_ids=libraries,
            model_selector=state.ctx.model,
        )
        allowed = set(libraries)
        deps = AgentRuntimeDeps(
            library_allowed=lambda library_id: library_id in allowed,
            emit_event=events.emit,
        )
        result = await get_runtime().run_turn(turn, state, events, deps)
        state.output = result.output
        state.citations = result.citations
        if result.token_usage is not None:
            state.token_usage = result.token_usage
    except asyncio.CancelledError:
        events.interrupted(state.output or "")
    except Exception as exc:
        log.exception("agent run %s failed", state.ctx.run_id)
        events.failed(f"{type(exc).__name__}: {exc}")
    finally:
        state.done.set()


def session_messages(profile_id: str, session_id: str) -> list[Any]:
    from viknow3.application.agentcore.runtime.session_messages import session_messages as _session_messages

    return _session_messages(profile_id, session_id)
