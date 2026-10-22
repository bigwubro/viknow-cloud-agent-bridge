"""A2A JSON-RPC client for remote agents (ADK). contextId is the ViKnow session id."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from viknow3.application.agentcore.runtime.events import RunEvents

A2A_TIMEOUT_SECONDS = 300.0


class A2AError(RuntimeError):
    pass


def _parts_text(parts: Any) -> str:
    texts: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("kind", part.get("type")) == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return "".join(texts)


def result_text(result: dict[str, Any]) -> str:
    """Text of a message/send result, which is either a Message or a Task."""
    if result.get("kind") == "message" or "parts" in result:
        return _parts_text(result.get("parts"))
    chunks = [_parts_text(artifact.get("parts")) for artifact in result.get("artifacts") or [] if isinstance(artifact, dict)]
    text = "".join(chunk for chunk in chunks if chunk)
    if text:
        return text
    message = (result.get("status") or {}).get("message") or {}
    return _parts_text(message.get("parts"))


async def send_message(
    endpoint: str,
    text: str,
    *,
    context_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": str(uuid4()),
                "contextId": context_id,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    async with httpx.AsyncClient(timeout=A2A_TIMEOUT_SECONDS, transport=transport) as client:
        response = await client.post(endpoint, json=body)
        response.raise_for_status()
        reply = response.json()
    if reply.get("error"):
        error = reply["error"]
        raise A2AError(f"A2A error {error.get('code')}: {error.get('message')}")
    return result_text(reply.get("result") or {})


async def run_a2a(endpoint: str, events: RunEvents, transport: httpx.AsyncBaseTransport | None = None) -> None:
    ctx = events.state.ctx
    events.emit("model_call_started")
    text = await send_message(endpoint, ctx.message, context_id=ctx.session_id, transport=transport)
    if text:
        events.emit("message_delta", {"text": text})
    events.completed(text, None)
