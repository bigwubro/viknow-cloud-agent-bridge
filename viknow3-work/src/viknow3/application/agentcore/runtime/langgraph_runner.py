"""Run one turn of a compiled LangGraph graph and map astream_events v2 to ViKnow events."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from viknow3.application.agentcore.harness.context import current_harness
from viknow3.application.agentcore.runtime.events import RunEvents

RECURSION_LIMIT = 60


def thread_config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": session_id}, "recursion_limit": RECURSION_LIMIT}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _top_level(event: dict[str, Any]) -> bool:
    """Events from the run's own graph, not from a subgraph called inside a tool."""
    namespace = str((event.get("metadata") or {}).get("langgraph_checkpoint_ns") or "")
    return "|" not in namespace


def _tool_output(output: Any) -> str:
    if isinstance(output, ToolMessage):
        return _text(output.content) or str(output.content)
    content = getattr(output, "content", None)
    if content is not None:
        return _text(content) or str(content)
    return "" if output is None else str(output)


def unanswered_tool_calls(messages: list[BaseMessage]) -> list[ToolMessage]:
    """Tool calls left open by an interrupted or failed turn. Chat APIs reject a history that has them."""
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    closing: list[ToolMessage] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = call.get("id")
            if call_id and call_id not in answered:
                closing.append(
                    ToolMessage(content="tool call did not finish (run interrupted)", tool_call_id=call_id, name=call.get("name"))
                )
    return closing


async def run_graph(graph: Any, events: RunEvents) -> None:
    ctx = events.state.ctx
    config = thread_config(ctx.session_id)
    snapshot = await graph.aget_state(config)
    history = list((snapshot.values or {}).get("messages") or []) if snapshot is not None else []
    inputs = {"messages": [*unanswered_tool_calls(history), HumanMessage(content=ctx.message)]}

    text = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    saw_usage = False
    streamed_calls: set[str] = set()
    try:
        async for event in graph.astream_events(inputs, config, version="v2"):
            if not _top_level(event):
                continue
            kind = event["event"]
            data = event.get("data") or {}
            if kind == "on_chat_model_start":
                events.emit("model_call_started")
            elif kind == "on_chat_model_stream":
                piece = _text(getattr(data.get("chunk"), "content", ""))
                if piece:
                    streamed_calls.add(event["run_id"])
                    text += piece
                    events.emit("message_delta", {"text": piece})
            elif kind == "on_chat_model_end":
                output = data.get("output")
                if event["run_id"] not in streamed_calls:
                    piece = _text(getattr(output, "content", ""))
                    if piece:
                        text += piece
                        events.emit("message_delta", {"text": piece})
                metadata = getattr(output, "usage_metadata", None) or {}
                if metadata:
                    saw_usage = True
                    for key in usage:
                        usage[key] += int(metadata.get(key) or 0)
            elif kind == "on_tool_start":
                events.emit(
                    "tool_call_started",
                    {"tool_call_id": event["run_id"], "name": event.get("name") or "", "arguments": data.get("input") or {}},
                )
            elif kind == "on_tool_end":
                output_text = _tool_output(data.get("output"))
                payload = {
                    "tool_call_id": event["run_id"],
                    "name": event.get("name") or "",
                    "text": output_text,
                }
                if '"tool_gate_blocked"' in output_text or '"code": "tool_gate_blocked"' in output_text:
                    payload["tool_gate"] = True
                events.emit("tool_call_completed", payload)
            elif kind == "on_tool_error":
                events.emit(
                    "tool_call_completed",
                    {
                        "tool_call_id": event["run_id"],
                        "name": event.get("name") or "",
                        "text": str(data.get("error") or "tool error"),
                        "is_error": True,
                    },
                )
    except BaseException:
        events.state.output = text or None
        raise
    harness = current_harness()
    if harness is not None:
        events.state.citations = harness.citations.export()
        for claim in harness.facts.verified_claims:
            harness.citations.add_verified_claim(claim)
    events.completed(text, usage if saw_usage else None)
