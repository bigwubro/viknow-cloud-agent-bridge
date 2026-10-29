"""Transitional runtime: same behavior as inlined knowledge_assistant, behind the adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from viknow_platform.application.agent.knowledge_assistant.answer_review import review_answer
from viknow_platform.application.agent.knowledge_assistant.bubblewrap_workspace import (
    capture_bubblewrap_workspace,
    restore_bubblewrap_workspace,
    with_artifacts_root,
)
from viknow_platform.application.agent.knowledge_assistant.builder import build_graph
from viknow_platform.application.agent.knowledge_assistant.answer_review import emit_answer_review
from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agent.knowledge_assistant.sandbox_runtime import create_bubblewrap_sandbox
from viknow_platform.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config
from viknow_platform.application.agent.knowledge_assistant.workspace_lifecycle import (
    finalize_session_run,
    prepare_session_run,
    workspace_runtime_instruction,
)
from viknow_platform.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow_platform.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult
from viknow_platform.application.agentcore.harness.citations import CitationRegistry
from viknow_platform.application.agentcore.harness.context import HarnessRunContext, bind_harness, reset_harness
from viknow_platform.application.agentcore.harness.facts import HarnessRunFacts
from viknow_platform.application.agentcore.environment.context import RunState
from viknow_platform.application.agentcore.runtime.checkpoint import checkpointer
from viknow_platform.application.agentcore.runtime.events import RunEvents
from viknow_platform.application.agentcore.runtime.langgraph_runner import run_graph

log = logging.getLogger(__name__)


class Viknow2CompatRuntime:
    """Today: viknow-platform re-implements viknow2. Tomorrow: delegate to viknow2 package."""

    async def run_turn(
        self,
        turn: AgentTurnInput,
        state: RunState,
        events: RunEvents,
        deps: AgentRuntimeDeps,
    ) -> AgentTurnResult:
        del deps  # grant/events already bound by host; factories wired at process level today.
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

        emit_answer_review(state, events, profile, harness_ctx)
        return AgentTurnResult(
            output=state.output or "",
            citations=state.citations or {},
            token_usage=state.token_usage,
        )


def get_runtime() -> Viknow2AgentRuntime:
    mode = __import__("os").environ.get("VIKNOW_AGENT_RUNTIME", "compat").strip().lower()
    if mode in {"compat", "viknow-platform", ""}:
        return Viknow2CompatRuntime()
    if mode in {"viknow2", "package"}:
        raise RuntimeError(
            "VIKNOW_AGENT_RUNTIME=viknow2 requires the viknow2 agent wheel; not installed in this build."
        )
    raise RuntimeError(f"unknown VIKNOW_AGENT_RUNTIME={mode}")
