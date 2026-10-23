"""Shared helpers for projecting persisted session events."""

from __future__ import annotations

from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.session.domain import ToolCallDebug


def payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def run_tool_calls(events: list[AgentEvent]) -> list[ToolCallDebug]:
    calls: dict[str, dict[str, str | None]] = {}
    order: list[str] = []
    for event in events:
        if event.type not in {
            AgentEventType.TOOL_CALL_STARTED,
            AgentEventType.TOOL_CALL_COMPLETED,
            AgentEventType.TOOL_CALL_FAILED,
        }:
            continue
        call_id = payload_text(event.payload, "id") or f"sequence-{event.sequence}"
        stream_id = payload_text(event.payload, "stream_id")
        lookup_id = stream_id if stream_id and stream_id in calls else call_id
        if lookup_id not in calls:
            order.append(lookup_id)
            calls[lookup_id] = {
                "id": call_id,
                "name": "",
                "status": "started",
                "arguments": "",
                "result": None,
                "error": None,
                "agent": None,
                "agent_name": None,
            }
        call = calls[lookup_id]
        call["id"] = call_id
        if stream_id:
            calls[stream_id] = call
        calls[call_id] = call
        name = payload_text(event.payload, "name")
        if name:
            call["name"] = name
        arguments = payload_text(event.payload, "arguments")
        if arguments:
            call["arguments"] = arguments
        agent = payload_text(event.payload, "agent")
        if agent:
            call["agent"] = agent
        agent_name = payload_text(event.payload, "agent_name")
        if agent_name:
            call["agent_name"] = agent_name
        if event.type == AgentEventType.TOOL_CALL_STARTED:
            call["status"] = "started"
        elif event.type == AgentEventType.TOOL_CALL_COMPLETED:
            call["status"] = "completed"
            call["result"] = payload_text(event.payload, "result")
        elif event.type == AgentEventType.TOOL_CALL_FAILED:
            call["status"] = "failed"
            call["error"] = payload_text(event.payload, "error")

    return [
        ToolCallDebug(
            id=str(call["id"] or ""),
            name=str(call["name"] or ""),
            status=str(call["status"] or ""),
            arguments=str(call["arguments"] or ""),
            result=call["result"],
            error=call["error"],
            agent=call["agent"],
            agent_name=call["agent_name"],
        )
        for call_id in order
        for call in [calls[call_id]]
    ]
