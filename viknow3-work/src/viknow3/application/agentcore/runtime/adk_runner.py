"""Run the in-process ADK agent (when not delegating to remote A2A)."""

from __future__ import annotations

import os

from google.genai import types

from viknow3.application.agent.knowledge_assistant.adk_builder import build_llm_agent
from viknow3.application.agent.knowledge_assistant.adk_events import AdkTurn, project_adk_event
from viknow3.application.agent.knowledge_assistant.adk_session import ensure_session
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.outbound.gateway import OutboundGateway
from viknow3.application.agentcore.runtime.events import RunEvents


async def run_adk(state: RunState, events: RunEvents, outbound: OutboundGateway) -> None:
    del outbound
    if os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() not in {"1", "true", "yes"}:
        events.failed(
            "in-process ADK is only enabled with VIKNOW_USE_SCRIPT_MODEL in this build; "
            "set VIKNOW_ADK_BASE_URL for remote ADK",
            code="adk_not_configured",
        )
        return

    from google.adk import Runner

    ctx = state.ctx
    await ensure_session(ctx.user_id, ctx.session_id)
    agent = build_llm_agent(ctx.model)
    from viknow3.application.agent.knowledge_assistant.adk_session import _session_service

    runner = Runner(agent=agent, app_name="viknow3_adk", session_service=_session_service())
    turn = AdkTurn()
    message = types.Content(role="user", parts=[types.Part(text=ctx.message)])
    events.emit("model_call_started")
    async for adk_event in runner.run_async(user_id=ctx.user_id, session_id=ctx.session_id, new_message=message):
        project_adk_event(events, turn, adk_event)
    events.completed(turn.text or "", None)
