"""Map one ADK turn onto the shell's run events.

The model endpoint and the knowledge client are resolved here. The ADK package
does not import this module or the DeepAgents runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from agents.adk_buddy.loop import run_turn as run_adk_turn
from agents.adk_buddy.notices import AdkNotice


@dataclass
class TurnResult:
    output: str = ""
    citations: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] | None = None


class AdkTurnRuntime:
    async def run_turn(self, turn: Any, state: Any, events: Any) -> TurnResult:
        from viknow3.application.hyperrag.runtime import get_engine_adapter
        from viknow3.ops.config.model_resolve import resolve_openai_chat

        from viknow3.application.agent.adk_adapter.inspect import RuntimeInspect

        resolved = resolve_openai_chat(getattr(turn, "model_selector", None))
        knowledge = get_engine_adapter()
        inspect = RuntimeInspect(
            knowledge,
            library_ids=tuple(turn.library_ids),
            question=turn.user_message,
            user_id=turn.user_id,
            workspace_id=turn.workspace_id or "",
            session_id=turn.session_id,
        )
        final_text = ""
        usage = None
        citations: dict[str, Any] = {}
        async for notice in run_adk_turn(
            message=turn.user_message,
            user_id=turn.user_id,
            workspace_id=turn.workspace_id,
            session_id=turn.session_id,
            library_ids=tuple(turn.library_ids),
            model=resolved.model,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            temperature=resolved.temperature,
            knowledge=knowledge,
            session_dsn=os.environ.get("VIKNOW_AGENT_DSN", ""),
            inspect=inspect,
        ):
            if notice.kind == "failed":
                events.failed(notice.text or "adk run failed")
                return TurnResult(output=state.output or "", citations=dict(state.citations or {}), token_usage=usage)
            _emit(notice, events, state)
            if notice.kind == "completed":
                final_text = notice.text
                usage = notice.token_usage
                citations = dict(notice.citations or {})
        if citations:
            state.citations = citations
        events.completed(final_text, usage if isinstance(usage, dict) else None)
        return TurnResult(output=final_text, citations=dict(state.citations or {}), token_usage=usage)


def _emit(notice: AdkNotice, events: Any, state: Any) -> None:
    if notice.kind == "model_started":
        events.emit("model_call_started")
    elif notice.kind == "tool_started":
        events.emit(
            "tool_call_started",
            {"tool_call_id": notice.tool_call_id, "name": notice.name, "arguments": notice.arguments},
        )
    elif notice.kind == "tool_completed":
        events.emit(
            "tool_call_completed",
            {
                "tool_call_id": notice.tool_call_id,
                "name": notice.name,
                "text": notice.text,
                **({"is_error": True} if notice.is_error else {}),
            },
        )
    elif notice.kind == "message_delta" and notice.text:
        events.emit("message_delta", {"text": notice.text})
    elif notice.kind == "completed" and notice.citations:
        state.citations = dict(notice.citations)


_RUNTIME = AdkTurnRuntime()


def get_runtime() -> AdkTurnRuntime:
    return _RUNTIME
