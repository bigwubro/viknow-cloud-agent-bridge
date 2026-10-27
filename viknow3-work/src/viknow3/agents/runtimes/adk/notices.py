"""Neutral turn notices. The shell maps these onto run events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdkNotice:
    kind: str
    name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    is_error: bool = False
    token_usage: dict[str, int] | None = None
    citations: dict[str, Any] | None = None


def notices_from_adk_event(event: Any) -> list[AdkNotice]:
    """Read one ADK event without importing google.adk."""
    if _error_text(event):
        return []
    partial = bool(getattr(event, "partial", False))
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    found: list[AdkNotice] = []
    for part in parts:
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None) and not partial:
            found.append(
                AdkNotice(
                    kind="tool_started",
                    name=str(call.name),
                    tool_call_id=str(getattr(call, "id", "") or ""),
                    arguments=_mapping(getattr(call, "args", None)),
                )
            )
        response = getattr(part, "function_response", None)
        if response is not None and not partial:
            found.append(
                AdkNotice(
                    kind="tool_completed",
                    name=str(getattr(response, "name", "") or ""),
                    tool_call_id=str(getattr(response, "id", "") or ""),
                    text=_response_text(getattr(response, "response", None)),
                )
            )
        text = getattr(part, "text", None)
        if text and not getattr(part, "thought", False):
            found.append(AdkNotice(kind="message_delta" if partial else "text", text=str(text)))
    return found


def final_text(event: Any) -> str | None:
    marker = getattr(event, "is_final_response", None)
    try:
        is_final = bool(marker()) if callable(marker) else False
    except Exception:
        is_final = False
    if not is_final:
        return None
    chunks = [
        notice.text
        for notice in notices_from_adk_event(event)
        if notice.kind == "text" and notice.text
    ]
    return "\n".join(chunks).strip()


def usage_from_event(event: Any) -> dict[str, int] | None:
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return None
    out: dict[str, int] = {}
    for source, dest in (
        ("prompt_token_count", "input_tokens"),
        ("candidates_token_count", "output_tokens"),
        ("total_token_count", "total_tokens"),
    ):
        value = getattr(usage, source, None)
        if isinstance(value, int):
            out[dest] = value
    return out or None


def error_text(event: Any) -> str:
    return _error_text(event)


def _error_text(event: Any) -> str:
    message = getattr(event, "error_message", None)
    if message:
        return str(message)
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        return {"value": str(value)}


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:4000]
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return str(value)[:4000]
