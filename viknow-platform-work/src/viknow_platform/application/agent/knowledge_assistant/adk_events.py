"""Map ADK stream events onto ViKnow run events (no ADK types in the access layer)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class _EventSink(Protocol):
    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None: ...


@dataclass
class AdkTurn:
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    _emitted_text: str = ""
    _model_open: bool = False
    _tool_ids: dict[str, str] = field(default_factory=dict)

    def _ensure_model_call(self, events: _EventSink) -> None:
        if not self._model_open:
            events.emit("model_call_started")
            self._model_open = True

    def _close_model_call(self) -> None:
        self._model_open = False


def _part_text(part: Any) -> str:
    text = getattr(part, "text", None)
    return str(text) if text else ""


def _function_call(part: Any) -> Any | None:
    return getattr(part, "function_call", None)


def _function_response(part: Any) -> Any | None:
    return getattr(part, "function_response", None)


def project_adk_event(events: _EventSink, turn: AdkTurn, event: Any) -> None:
    """Project one ADK event. Partial text deltas are deduplicated against turn.text."""
    content = getattr(event, "content", None)
    parts = list(getattr(content, "parts", None) or [])
    partial = bool(getattr(event, "partial", False))
    usage = getattr(event, "usage_metadata", None)

    for part in parts:
        call = _function_call(part)
        if call is not None:
            turn._ensure_model_call(events)
            tool_id = str(getattr(call, "id", "") or "")
            name = str(getattr(call, "name", "") or "")
            args = getattr(call, "args", None) or {}
            turn._tool_ids[tool_id] = tool_id
            events.emit(
                "tool_call_started",
                {"tool_call_id": tool_id, "name": name, "arguments": args},
            )
            continue

        response = _function_response(part)
        if response is not None:
            tool_id = str(getattr(response, "id", "") or "")
            name = str(getattr(response, "name", "") or "")
            payload = getattr(response, "response", None)
            if isinstance(payload, dict):
                text = json.dumps(payload, ensure_ascii=False)
            else:
                text = str(payload or "")
            events.emit(
                "tool_call_completed",
                {"tool_call_id": tool_id, "name": name, "text": text},
            )
            turn._close_model_call()
            continue

        chunk = _part_text(part)
        if not chunk:
            continue

        if partial:
            turn._ensure_model_call(events)
            turn.text += chunk
            if len(turn.text) > len(turn._emitted_text):
                delta = turn.text[len(turn._emitted_text) :]
                turn._emitted_text = turn.text
                events.emit("message_delta", {"text": delta})
            continue

        turn._ensure_model_call(events)
        new_text = chunk if chunk.startswith(turn.text) or not turn.text else turn.text + chunk
        if len(new_text) > len(turn._emitted_text):
            delta = new_text[len(turn._emitted_text) :]
            turn._emitted_text = new_text
            events.emit("message_delta", {"text": delta})
        turn.text = new_text

    if not partial and usage is not None:
        turn.usage = {
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "completion_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        }
        if turn.text:
            turn._close_model_call()
            events.emit("message_completed", {"text": turn.text, "token_usage": turn.usage})
