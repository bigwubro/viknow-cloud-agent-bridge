"""One ADK turn. Session state stays in ADK's own session service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from agents.adk_buddy.instruction import build_instruction
from agents.adk_buddy.knowledge_tools import CitationLog, build_knowledge_tools
from agents.adk_buddy.notices import AdkNotice, error_text, final_text, notices_from_adk_event, usage_from_event
from agents.adk_buddy.session_url import async_postgres_url

log = logging.getLogger(__name__)

APP_NAME = "viknow-adk"
_SERVICES: dict[str, Any] = {}


async def run_turn(
    *,
    message: str,
    user_id: str,
    workspace_id: str | None,
    session_id: str,
    library_ids: tuple[str, ...],
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    knowledge: Any,
    session_dsn: str,
) -> AsyncIterator[AdkNotice]:
    """Yield tool and answer notices. The caller writes shell events."""
    citations = CitationLog()
    answer: list[str] = []
    streamed: list[str] = []
    usage: dict[str, int] | None = None
    try:
        runner, content, adk_user, adk_session = _prepare(
            message=message,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            library_ids=library_ids,
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            knowledge=knowledge,
            citations=citations,
            session_dsn=session_dsn,
            question=message,
        )
        await _ensure_session(runner, adk_user, adk_session)
        yield AdkNotice(kind="model_started")
        async for event in runner.run_async(
            user_id=adk_user,
            session_id=adk_session,
            new_message=content,
        ):
            message_error = error_text(event)
            if message_error:
                yield AdkNotice(kind="failed", text=message_error)
                return
            usage = usage_from_event(event) or usage
            closed = final_text(event)
            for notice in notices_from_adk_event(event):
                if notice.kind == "text":
                    answer.append(notice.text)
                elif notice.kind == "message_delta":
                    streamed.append(notice.text)
                    yield notice
                else:
                    yield notice
            if closed:
                answer = [closed]
    except Exception as exc:
        log.exception("adk turn failed")
        yield AdkNotice(kind="failed", text=f"{type(exc).__name__}: {exc}")
        return
    yield AdkNotice(
        kind="completed",
        text=citations.submitted_answer
        or "\n".join(part for part in (answer or streamed) if part).strip(),
        token_usage=usage,
        citations={"evidence": list(citations.rows)},
    )


def _prepare(
    *,
    message: str,
    user_id: str,
    workspace_id: str | None,
    session_id: str,
    library_ids: tuple[str, ...],
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    knowledge: Any,
    citations: CitationLog,
    session_dsn: str,
    question: str,
) -> tuple[Any, Any, str, str]:
    from google.adk.agents.llm_agent import Agent
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.runners import Runner
    from google.genai import types

    endpoint = LiteLlm(
        model=f"openai/{model}",
        api_base=base_url,
        api_key=api_key,
        temperature=temperature,
        drop_params=True,
    )
    agent = Agent(
        name="adk_buddy",
        model=endpoint,
        instruction=build_instruction(library_ids),
        tools=build_knowledge_tools(knowledge, library_ids, citations, question),
    )
    service = _session_service(session_dsn)
    try:
        runner = Runner(app_name=APP_NAME, agent=agent, session_service=service, auto_create_session=True)
    except TypeError:
        runner = Runner(app_name=APP_NAME, agent=agent, session_service=service)
    content = types.Content(role="user", parts=[types.Part(text=message)])
    adk_user = user_id or "anonymous"
    adk_session = f"{workspace_id or 'default'}:{session_id}"
    return runner, content, adk_user, adk_session


def _session_service(dsn: str) -> Any:
    key = dsn.strip() or "memory"
    cached = _SERVICES.get(key)
    if cached is not None:
        return cached
    if not dsn.strip():
        from google.adk.sessions import InMemorySessionService

        service = InMemorySessionService()
    else:
        from google.adk.sessions import DatabaseSessionService

        service = DatabaseSessionService(db_url=async_postgres_url(dsn))
    _SERVICES[key] = service
    return service


async def _ensure_session(runner: Any, user_id: str, session_id: str) -> None:
    service = runner.session_service
    found = await service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    if found is None:
        await service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
