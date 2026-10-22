"""Run the in-process ADK agent (when not delegating to remote A2A)."""

from __future__ import annotations

from google.genai import types

from viknow3.application.agent.knowledge_assistant.adk_builder import build_llm_agent
from viknow3.application.agent.knowledge_assistant.instructions import runtime_instruction
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.qa_expert.grant import bind_user_message, current_grant, reset_user_message
from viknow3.application.agent.knowledge_assistant.adk_events import AdkTurn, project_adk_event
from viknow3.application.agent.knowledge_assistant.adk_session import ensure_session
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.outbound.gateway import OutboundGateway
from viknow3.application.agentcore.runtime.events import RunEvents


async def run_adk(state: RunState, events: RunEvents, outbound: OutboundGateway) -> None:
    del outbound
    from google.adk import Runner

    ctx = state.ctx
    msg_token = bind_user_message(ctx.message)
    await ensure_session(ctx.user_id, ctx.session_id)
    profile = load_viknow_v2_profile()
    instruction = runtime_instruction(profile, "adk")
    grant = current_grant()
    if grant is not None:
        libraries = [str(item) for item in grant.libraries()]
        if libraries:
            instruction = (
                f"{instruction}\n\n本轮授权知识库 library_id：{', '.join(libraries)}。"
                " 请对这些库调用 knowledge_search 获取证据后再回答。"
            )
    agent = build_llm_agent(ctx.model, instruction=instruction)
    from viknow3.application.agent.knowledge_assistant.adk_session import _session_service

    runner = Runner(agent=agent, app_name="viknow3_adk", session_service=_session_service())
    turn = AdkTurn()
    message = types.Content(role="user", parts=[types.Part(text=ctx.message)])
    events.emit("model_call_started")
    try:
        async for adk_event in runner.run_async(user_id=ctx.user_id, session_id=ctx.session_id, new_message=message):
            project_adk_event(events, turn, adk_event)
        events.completed(turn.text or "", None)
    finally:
        reset_user_message(msg_token)
