"""Map viknow AgentEvent stream into viknow3 RunEvents."""

from __future__ import annotations

from typing import Any

from agents.runtime.core.runtime.domain import AgentEvent, AgentEventType

from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.events import RunEvents

_SKIP_TYPES = frozenset(
    {
        AgentEventType.RUN_STARTED,
        AgentEventType.USER_MESSAGE,
    }
)


def apply_vendor_event(event: AgentEvent, events: RunEvents, state: RunState) -> str | None:
    """Forward one DeepAgents event. Returns terminal status when the run ends."""
    if event.type in _SKIP_TYPES:
        return None
    payload = dict(event.payload or {})
    if event.type == AgentEventType.MESSAGE_DELTA:
        text = str(payload.get("text") or "")
        if text:
            events.emit("message_delta", {"text": text})
    elif event.type == AgentEventType.MODEL_CALL_STARTED:
        events.emit("model_call_started")
    elif event.type == AgentEventType.TOOL_CALL_STARTED:
        events.emit(
            "tool_call_started",
            {
                "tool_call_id": str(payload.get("id") or payload.get("tool_call_id") or ""),
                "name": str(payload.get("name") or ""),
                "arguments": payload.get("arguments") or {},
            },
        )
    elif event.type == AgentEventType.TOOL_CALL_COMPLETED:
        events.emit(
            "tool_call_completed",
            {
                "tool_call_id": str(payload.get("id") or payload.get("tool_call_id") or ""),
                "name": str(payload.get("name") or ""),
                "text": str(payload.get("text") or payload.get("result") or ""),
            },
        )
    elif event.type == AgentEventType.TOOL_CALL_FAILED:
        events.emit(
            "tool_call_completed",
            {
                "tool_call_id": str(payload.get("id") or payload.get("tool_call_id") or ""),
                "name": str(payload.get("name") or ""),
                "text": str(payload.get("message") or payload.get("error") or "tool error"),
                "is_error": True,
            },
        )
    elif event.type == AgentEventType.ANSWER_REVIEW_STARTED:
        events.emit("answer_review_started", payload)
    elif event.type == AgentEventType.ANSWER_REVIEW_COMPLETED:
        events.emit("answer_review_completed", payload)
    elif event.type == AgentEventType.MESSAGE_COMPLETED:
        text = str(payload.get("text") or "")
        citations = payload.get("citations")
        if isinstance(citations, dict):
            state.citations = citations
        usage = payload.get("token_usage")
        if isinstance(usage, dict):
            state.token_usage = usage
        if text and not state.output:
            state.output = text
    elif event.type == AgentEventType.RUN_COMPLETED:
        if not state.output:
            state.output = str(payload.get("message") or "")
        events.completed(state.output or "", state.token_usage if isinstance(state.token_usage, dict) else None)
        return "completed"
    elif event.type == AgentEventType.RUN_FAILED:
        message = str(payload.get("message") or "run failed")
        events.failed(message)
        return "failed"
    elif event.type == AgentEventType.RUN_INTERRUPTED:
        events.interrupted(state.output or "")
        return "interrupted"
    return None
