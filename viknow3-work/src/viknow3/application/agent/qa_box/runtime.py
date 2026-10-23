"""Black-box QA agent turn: viknow-v2 deepagents graph + workspace + harness."""

from __future__ import annotations

import asyncio
import logging

from viknow3.application.agent.qa_box.answer_review import review_answer
from viknow3.application.agent.qa_box.bubblewrap_workspace import (
    capture_bubblewrap_workspace,
    restore_bubblewrap_workspace,
    with_artifacts_root,
)
from viknow3.application.agent.qa_box.builder import build_graph
from viknow3.application.agent.qa_box.harness.citations import CitationRegistry
from viknow3.application.agent.qa_box.harness.context import HarnessRunContext, bind_harness, reset_harness
from viknow3.application.agent.qa_box.harness.facts import HarnessRunFacts
from viknow3.application.agent.qa_box.profile import load_viknow_v2_profile
from viknow3.application.agent.qa_box.sandbox_runtime import create_bubblewrap_sandbox
from viknow3.application.agent.qa_box.workspace_config import load_deepagents_workspace_config
from viknow3.application.agent.qa_box.workspace_lifecycle import (
    finalize_session_run,
    prepare_session_run,
    workspace_runtime_instruction,
)
from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.checkpoint import checkpointer
from viknow3.application.agentcore.runtime.events import RunEvents
from viknow3.application.agentcore.runtime.langgraph_runner import run_graph

log = logging.getLogger(__name__)


def _emit_answer_review(state: RunState, events: RunEvents, profile, harness_ctx: HarnessRunContext) -> None:
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


class QaBoxRuntime:
    async def run_turn(
        self,
        turn: AgentTurnInput,
        state: RunState,
        events: RunEvents,
        deps: AgentRuntimeDeps,
    ) -> AgentTurnResult:
        del deps  # grant/events bound by host; HyperRAG/Actio via viknow3 clients inside tools.
        cfg = load_deepagents_workspace_config()
        profile = load_viknow_v2_profile()
        session_id = turn.session_id
        ws_ctx = prepare_session_run(session_id, profile.enabled_skills)
        workspace_dir = ws_ctx.root
        libraries = list(turn.library_ids)
        workspace_overlay = workspace_runtime_instruction(ws_ctx, libraries, "deepagents")
        primary_skill = profile.enabled_skills[0] if profile.enabled_skills else "knowledge-qa-v2"
        harness_ctx = HarnessRunContext(
            facts=HarnessRunFacts(library_ids=libraries),
            primary_skill=primary_skill,
            citations=CitationRegistry(),
            emit=events.emit,
        )
        harness_token = bind_harness(harness_ctx)
        saver = await checkpointer()

        try:
            if cfg.backend != "bubblewrap":
                graph = await asyncio.to_thread(
                    build_graph,
                    turn.model_selector or state.ctx.model,
                    saver,
                    session_id,
                    backend=None,
                    workspace_overlay=workspace_overlay,
                )
                await run_graph(graph, events)
            else:
                runtime = await asyncio.to_thread(create_bubblewrap_sandbox)
                setup_ok = False
                try:
                    await asyncio.to_thread(restore_bubblewrap_workspace, runtime.backend, workspace_dir)
                    setup_ok = True
                    backend = with_artifacts_root(runtime.backend)
                    graph = await asyncio.to_thread(
                        build_graph,
                        turn.model_selector or state.ctx.model,
                        saver,
                        session_id,
                        backend=backend,
                        workspace_overlay=workspace_overlay,
                    )
                    await run_graph(graph, events)
                finally:
                    try:
                        if setup_ok:
                            await asyncio.to_thread(
                                capture_bubblewrap_workspace,
                                runtime.backend,
                                workspace_dir,
                                session_id,
                            )
                    except Exception:
                        log.exception("bubblewrap capture failed for session %s", session_id)
                        raise
                    finally:
                        if runtime.close is not None:
                            runtime.close()
        finally:
            reset_harness(harness_token)
            await asyncio.to_thread(finalize_session_run, session_id)

        _emit_answer_review(state, events, profile, harness_ctx)
        return AgentTurnResult(
            output=state.output or "",
            citations=state.citations or {},
            token_usage=state.token_usage,
        )


_RUNTIME = QaBoxRuntime()


def get_runtime() -> Viknow2AgentRuntime:
    return _RUNTIME
