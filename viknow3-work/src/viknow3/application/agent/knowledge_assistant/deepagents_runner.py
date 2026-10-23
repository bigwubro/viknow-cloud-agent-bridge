"""One deepagents turn — delegates to viknow2_adapter (black-box boundary)."""

from __future__ import annotations

import logging
from typing import Any

from viknow3.application.agent.knowledge_assistant.answer_review import review_answer
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.viknow2_adapter import AgentRuntimeDeps, AgentTurnInput, get_runtime
from viknow3.application.agentcore.harness.context import HarnessRunContext
from viknow3.application.agent.qa_expert.grant import current_grant
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.events import RunEvents

log = logging.getLogger(__name__)


def _emit_answer_review(state: RunState, events: RunEvents, profile: Any, harness_ctx: HarnessRunContext) -> None:
    text = state.output or ""
    citations = harness_ctx.citations.export()
    state.citations = citations
    events.emit("answer_review_started", {"library_ids": list(harness_ctx.facts.library_ids)})
    result = review_answer(
        text,
        profile.answer_review,
        library_ids=list(harness_ctx.facts.library_ids),
        search_called=harness_ctx.facts.search_called,
        citations=citations,
    )
    events.emit(
        "answer_review_completed",
        {"passed": result.passed, "score": result.score, "reason": result.reason},
    )
    if result.output != text:
        state.output = result.output


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
