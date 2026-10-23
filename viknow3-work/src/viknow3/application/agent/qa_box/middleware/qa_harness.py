"""DeepAgents middleware: skill load + tool gate + QA final check.

TRANSITIONAL — not in viknow2 main. Replace with vendored viknow.agent_adapters.deepagents
middleware stack after run_turn delegates to DeepAgentsRuntime. See docs/viknow-agent-vendor.md.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT
from langchain_core.messages import ToolMessage

from viknow3.application.agent.qa_box.library_scope import resolve_library_id
from viknow3.application.agent.qa_box.harness.context import current_harness
from viknow3.application.agent.qa_box.harness.envelope import tool_gate_blocked
from viknow3.application.agent.qa_box.harness.skill_reminder import render_skill_reminder
from viknow3.application.agent.qa_box.harness.tool_gate import (
    LIBRARY_ARG_TOOLS,
    WORKFLOW_TOOLS,
    skill_path_matches,
    evaluate_tool_gate,
)


def _tool_name(request: Any) -> str:
    if request.tool is not None:
        return str(request.tool.name)
    return str(request.tool_call.get("name") or "")


def _tool_args(request: Any) -> dict[str, Any]:
    raw = request.tool_call.get("args") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_tool_json(content: Any) -> dict[str, Any] | None:
    text = content if isinstance(content, str) else str(content or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _annotate_citations(tool_name: str, payload: dict[str, Any], registry: Any) -> dict[str, Any]:
    if tool_name != "knowledge_search":
        return payload
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        return payload
    annotated: list[Any] = []
    for item in evidence:
        if isinstance(item, dict):
            tagged = registry.register_evidence(item, source_tool=tool_name)
            merged = {**item, **tagged}
            annotated.append(merged)
        else:
            annotated.append(item)
    payload = {**payload, "evidence": annotated}
    return payload


def _update_facts_after_tool(tool_name: str, args: dict[str, Any], content: Any, facts: Any, primary_skill: str) -> None:
    if tool_name == "read_file":
        path = str(args.get("path") or args.get("file_path") or "")
        if skill_path_matches(path, primary_skill):
            facts.skill_loaded = True
        return
    payload = _parse_tool_json(content)
    if payload is None:
        return
    if payload.get("code") == "tool_gate_blocked":
        return
    if tool_name == "knowledge_discover":
        facts.discover_called = True
        for row in payload.get("libraries") or []:
            if isinstance(row, dict) and row.get("library_id"):
                facts.selected_library_ids.add(str(row["library_id"]))
    if tool_name == "knowledge_search":
        facts.search_called = True
    if tool_name in {"query_refine", "answer_plan"}:
        facts.plan_recorded = True


class QaHarnessMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """A1 + A2 + A3 (light) + A10: skill load, tool gate, model reminder, fact tracking."""

    def __init__(self, *, primary_skill: str, skill_path: str, skill_reminder_enabled: bool = True) -> None:
        super().__init__()
        self._primary_skill = primary_skill
        self._skill_path = skill_path
        self._skill_reminder_enabled = skill_reminder_enabled

    def _augment_model_request(self, request: Any) -> Any:
        harness = current_harness()
        if harness is not None and self._skill_reminder_enabled:
            reminder = render_skill_reminder(self._primary_skill, self._skill_path)
            system = getattr(request, "system_message", None)
            if system is not None:
                content = getattr(system, "content", "") or ""
                if isinstance(content, str) and reminder.strip() not in content:
                    request = request.override(system_message=type(system)(content=content + reminder))
        if harness is not None and harness.facts.library_ids and not harness.facts.search_called:
            messages = list(getattr(request, "messages", []) or [])
            if messages and not getattr(messages[-1], "tool_calls", None):
                system = getattr(request, "system_message", None)
                extra = "\n[Harness] 已授权知识库但尚未 knowledge_search；继续调用检索工具后再作答。\n"
                if system is not None:
                    content = getattr(system, "content", "") or ""
                    if isinstance(content, str):
                        request = request.override(system_message=type(system)(content=content + extra))
        return request

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._augment_model_request(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._augment_model_request(request))

    def wrap_tool_call(self, request: Any, handler: Any) -> ToolMessage | Any:
        harness = current_harness()
        tool_name = _tool_name(request)
        args = _tool_args(request)
        if harness is not None:
            decision = evaluate_tool_gate(tool_name, args, harness.facts, primary_skill=self._primary_skill)
            if decision is not None:
                if harness.emit is not None:
                    harness.emit(
                        "tool_gate_blocked",
                        {"tool": tool_name, "rule": decision.rule, "message": decision.message},
                    )
                return ToolMessage(
                    content=tool_gate_blocked(tool_name, decision.rule, decision.message, decision.recovery),
                    tool_call_id=request.tool_call["id"],
                    name=tool_name,
                    status="error",
                )
            if tool_name in LIBRARY_ARG_TOOLS and "library_id" in args:
                resolved = resolve_library_id(str(args.get("library_id") or ""))
                if resolved:
                    merged = {**args, "library_id": resolved}
                    request = request.override(tool_call={**request.tool_call, "args": merged})
        result = handler(request)
        if harness is not None and isinstance(result, ToolMessage):
            content = result.content
            payload = _parse_tool_json(content)
            if payload is not None and tool_name in WORKFLOW_TOOLS:
                payload = _annotate_citations(tool_name, payload, harness.citations)
                content = json.dumps(payload, ensure_ascii=False)
                result = ToolMessage(
                    content=content,
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                    status=result.status,
                )
            _update_facts_after_tool(tool_name, args, content, harness.facts, self._primary_skill)
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> ToolMessage | Any:
        harness = current_harness()
        tool_name = _tool_name(request)
        args = _tool_args(request)
        if harness is not None:
            decision = evaluate_tool_gate(tool_name, args, harness.facts, primary_skill=self._primary_skill)
            if decision is not None:
                if harness.emit is not None:
                    harness.emit(
                        "tool_gate_blocked",
                        {"tool": tool_name, "rule": decision.rule, "message": decision.message},
                    )
                return ToolMessage(
                    content=tool_gate_blocked(tool_name, decision.rule, decision.message, decision.recovery),
                    tool_call_id=request.tool_call["id"],
                    name=tool_name,
                    status="error",
                )
            if tool_name in LIBRARY_ARG_TOOLS and "library_id" in args:
                resolved = resolve_library_id(str(args.get("library_id") or ""))
                if resolved:
                    merged = {**args, "library_id": resolved}
                    request = request.override(tool_call={**request.tool_call, "args": merged})
        result = await handler(request)
        if harness is not None and isinstance(result, ToolMessage):
            content = result.content
            payload = _parse_tool_json(content)
            if payload is not None and tool_name in WORKFLOW_TOOLS:
                payload = _annotate_citations(tool_name, payload, harness.citations)
                content = json.dumps(payload, ensure_ascii=False)
                result = ToolMessage(
                    content=content,
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                    status=result.status,
                )
            _update_facts_after_tool(tool_name, args, content, harness.facts, self._primary_skill)
        return result
