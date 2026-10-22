"""One deepagents turn with viknow2 bubblewrap sandbox lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from viknow3.application.agent.knowledge_assistant.bubblewrap_workspace import (
    capture_bubblewrap_workspace,
    prepare_host_staging,
    restore_bubblewrap_workspace,
    with_artifacts_root,
)
from viknow3.application.agent.knowledge_assistant.builder import build_graph
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.sandbox_runtime import create_bubblewrap_sandbox
from viknow3.application.agent.knowledge_assistant.workspace_config import load_deepagents_workspace_config
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.checkpoint import checkpointer
from viknow3.application.agentcore.runtime.events import RunEvents
from viknow3.application.agentcore.runtime.langgraph_runner import run_graph

log = logging.getLogger(__name__)


async def run_deepagents_turn(state: RunState, events: RunEvents) -> None:
    cfg = load_deepagents_workspace_config()
    profile = load_viknow_v2_profile()
    session_id = state.ctx.session_id
    workspace_dir = prepare_host_staging(session_id, profile.enabled_skills)
    saver = await checkpointer()

    if cfg.backend != "bubblewrap":
        graph = await asyncio.to_thread(build_graph, state.ctx.model, saver, session_id, backend=None)
        await run_graph(graph, events)
        return

    runtime = await asyncio.to_thread(create_bubblewrap_sandbox)
    setup_ok = False
    try:
        await asyncio.to_thread(restore_bubblewrap_workspace, runtime.backend, workspace_dir)
        setup_ok = True
        backend = with_artifacts_root(runtime.backend)
        graph = await asyncio.to_thread(
            build_graph,
            state.ctx.model,
            saver,
            session_id,
            backend=backend,
        )
        await run_graph(graph, events)
    finally:
        if setup_ok:
            try:
                await asyncio.to_thread(
                    capture_bubblewrap_workspace,
                    runtime.backend,
                    workspace_dir,
                    session_id,
                )
            except Exception:
                log.exception("bubblewrap capture failed for session %s", session_id)
                raise
        if runtime.close is not None:
            runtime.close()
