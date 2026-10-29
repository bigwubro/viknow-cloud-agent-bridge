"""Run the in-process ADK agent (when not delegating to remote A2A)."""

from __future__ import annotations

from google.genai import types

from viknow_platform.application.agent.knowledge_assistant.adk_builder import build_llm_agent
from viknow_platform.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agent.knowledge_assistant.workspace_lifecycle import (
    finalize_session_run,
    prepare_session_run,
    workspace_runtime_instruction,
)
from viknow_platform.application.agent.qa_expert.grant import bind_user_message, current_grant, reset_user_message
from viknow_platform.application.agent.knowledge_assistant.adk_events import AdkTurn, project_adk_event
from viknow_platform.application.agent.knowledge_assistant.adk_session import ensure_session
from viknow_platform.application.agentcore.environment.context import RunState
from viknow_platform.application.agentcore.outbound.gateway import OutboundGateway
from viknow_platform.application.agentcore.runtime.events import RunEvents


async def run_adk(state: RunState, events: RunEvents, outbound: OutboundGateway) -> None:
    del outbound
    from google.adk import Runner

    ctx = state.ctx
    msg_token = bind_user_message(ctx.message)
    await ensure_session(ctx.user_id, ctx.session_id)
    profile = load_viknow_v2_profile()
    workspace_ctx = prepare_session_run(ctx.session_id, profile.enabled_skills)
    grant = current_grant()
    libraries: list[str] = []
    if grant is not None:
        libraries = [str(item) for item in grant.libraries()]
    instruction = runtime_instruction(profile, "adk") + workspace_runtime_instruction(
        workspace_ctx, libraries, "adk"
    )
    agent = build_llm_agent(
        ctx.model,
        instruction=instruction,
        session_workspace_root=workspace_ctx.root,
        session_skills_dir=workspace_ctx.skills_dir,
    )
    from viknow_platform.application.agent.knowledge_assistant.adk_session import _session_service

    runner = Runner(agent=agent, app_name="viknow_platform_adk", session_service=_session_service())
    turn = AdkTurn()
    message = types.Content(role="user", parts=[types.Part(text=ctx.message)])
    events.emit("model_call_started")
    try:
        async for adk_event in runner.run_async(user_id=ctx.user_id, session_id=ctx.session_id, new_message=message):
            project_adk_event(events, turn, adk_event)
        events.completed(turn.text or "", None)
    finally:
        finalize_session_run(ctx.session_id)
        reset_user_message(msg_token)
