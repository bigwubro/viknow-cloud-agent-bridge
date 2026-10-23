"""Outer shell entry: one QA turn via the black box."""

from __future__ import annotations

from viknow3.application.agent.viknow2_adapter import AgentRuntimeDeps, AgentTurnInput, get_runtime
from viknow3.application.agent.qa_expert.grant import current_grant
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.events import RunEvents


async def run_deepagents_turn(state: RunState, events: RunEvents) -> None:
    grant = current_grant()
    libraries = tuple(grant.libraries()) if grant is not None else ()
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
