"""ViKnow-owned tools exposed to DeepAgents."""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import StructuredTool
from loguru import logger

from agents.expert_buddy.agent_adapters.langfuse import (
    get_active_tool_scope,
    publish_pending_tool_token_usage,
    reset_active_tool,
    set_active_tool,
)
from agents.expert_buddy.core.multimodal.structured_llm import (
    collect_structured_llm_usage,
    structured_llm_usage_summary,
)
from agents.expert_buddy.core.observability.otel import (
    ERROR_TOOL_EXECUTION_TIMEOUT,
    record_error,
)
from agents.expert_buddy.core.runtime.domain import AgentProfile
from agents.expert_buddy.core.runtime.tool_gate import ToolGateContext, evaluate_tool_gate
from agents.expert_buddy.core.scheduler.context import get_source_id
from agents.expert_buddy.core.scheduler.domain import (
    ExecUnit,
    Priority,
    RunBudgetCancelledError,
    ToolQueueTimeoutError,
)
from agents.expert_buddy.core.scheduler.metrics_events import record_tool_failure, record_tool_timeout
from agents.expert_buddy.core.scheduler.resource_scheduler import ResourceScheduler
from agents.expert_buddy.core.tools import ToolSpec, ViKnowToolHandlers, build_viknow_tool_specs
from agents.expert_buddy.core.tools.base import ToolErrorCode, ToolResult, summarize_tool_result
from agents.expert_buddy.core.tools.context import (
    get_tool_run_context,
    reset_knowledge_search_budget,
    with_run_question,
)
from agents.expert_buddy.core.tools.executor import run_tool_sync

DEEPAGENTS_NATIVE_TOOL_IDS = frozenset(
    {"read_file", "write_file", "edit_file", "ls", "glob", "grep", "execute"}
)


def build_deepagents_tools(
    profile: AgentProfile,
    *,
    tool_handlers: ViKnowToolHandlers | None = None,
    tool_specs: tuple[ToolSpec, ...] | None = None,
    scheduler: ResourceScheduler | None = None,
) -> list[StructuredTool]:
    enabled = set(profile.enabled_tools)
    if not enabled:
        return []
    resolved_tool_specs = (
        tool_specs if tool_specs is not None else build_viknow_tool_specs(tool_handlers)
    )
    supported = {spec.id for spec in resolved_tool_specs}
    unknown = sorted(enabled - supported - DEEPAGENTS_NATIVE_TOOL_IDS)
    if unknown:
        raise RuntimeError(f"unsupported DeepAgents tool(s): {', '.join(unknown)}")
    return [
        _to_structured_tool(spec, scheduler=scheduler, profile_id=profile.id)
        for spec in resolved_tool_specs
        if spec.id in enabled
    ]


def _to_structured_tool(
    spec: ToolSpec,
    *,
    scheduler: ResourceScheduler | None = None,
    profile_id: str | None = None,
) -> StructuredTool:
    def invoke(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        tool_token = set_active_tool(spec.name)
        try:
            if spec.name != "knowledge_search":
                reset_knowledge_search_budget()
            blocked = _tool_gate_blocked_result(spec.name, dict(kwargs))
            if blocked is not None:
                return _finalize_tool_result(spec.name, blocked, [])
            with collect_structured_llm_usage() as usage_records:
                result = spec.handler(with_run_question(spec.name, dict(kwargs)))
            return _finalize_tool_result(spec.name, result, usage_records)
        finally:
            reset_active_tool(tool_token)

    async def ainvoke(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        tool_token = set_active_tool(spec.name)
        try:
            if spec.name != "knowledge_search":
                reset_knowledge_search_budget()
            blocked = _tool_gate_blocked_result(spec.name, dict(kwargs))
            if blocked is not None:
                return _finalize_tool_result(spec.name, blocked, [])
            with collect_structured_llm_usage() as usage_records:
                result = await _ainvoke_tool_content(
                    with_run_question(spec.name, dict(kwargs)),
                    spec,
                    scheduler,
                    profile_id=profile_id,
                )
            return _finalize_tool_result(spec.name, result, usage_records)
        finally:
            reset_active_tool(tool_token)

    return StructuredTool.from_function(
        func=invoke,
        coroutine=ainvoke,
        name=spec.name,
        description=spec.description,
        args_schema=spec.schema["parameters"],
        infer_schema=False,
        response_format="content_and_artifact",
    )


def _tool_gate_blocked_result(tool_name: str, arguments: dict[str, Any]) -> str | None:
    run_context = get_tool_run_context()
    if run_context is None or run_context.tool_gate_context_factory is None:
        return None
    gate_context = run_context.tool_gate_context_factory()
    if not isinstance(gate_context, ToolGateContext):
        raise RuntimeError("tool gate context factory returned an invalid context")
    decision = evaluate_tool_gate(gate_context, tool_name=tool_name, arguments=arguments)
    if decision.allowed:
        return None
    return ToolResult.fail(
        tool_name,
        ToolErrorCode.GATE_BLOCKED,
        decision.next_action,
        detail=decision.reason_code,
        data={
            "tool_gate": {
                "blocked": True,
                "reason_code": decision.reason_code,
                "next_action": decision.next_action,
                "recovery": decision.recovery.as_payload() if decision.recovery else None,
            },
        },
    ).to_json()


async def _ainvoke_tool_content(
    kwargs: dict[str, Any],
    spec: ToolSpec,
    scheduler: ResourceScheduler | None,
    *,
    profile_id: str | None = None,
) -> str:
    if scheduler is not None:
        source_id = get_source_id()
        if scheduler.is_budget_exhausted(source_id):
            return _budget_cancelled_tool_error_payload(
                spec,
                dimensions=scheduler.budget_cancel_dimensions(source_id),
            )

    async def invoke() -> str:
        if spec.async_handler is not None:
            return await spec.async_handler(kwargs)
        return await run_tool_sync(spec.handler, kwargs)

    if scheduler is None:
        return await _invoke_with_timeout(invoke(), spec, profile_id=profile_id)

    timeout = (
        spec.execution_timeout_seconds
        if spec.execution_timeout_seconds is not None
        else scheduler.default_execution_timeout_seconds
    )
    unit = ExecUnit(
        source_id=get_source_id(),
        priority=Priority.INTERACTIVE,
        resources=(spec.resource_class,) if spec.resource_class else (),
        handler=invoke,
        timeout=timeout,
        metadata={"tool_id": spec.id, "tool_name": spec.name},
    )
    try:
        result = await scheduler.run(unit)
    except RunBudgetCancelledError as exc:
        return _budget_cancelled_tool_error_payload(spec, dimensions=exc.dimensions)
    except ToolQueueTimeoutError as exc:
        return _queue_timeout_tool_error_payload(spec, str(exc))
    except TimeoutError:
        record_error(ERROR_TOOL_EXECUTION_TIMEOUT)
        record_tool_timeout(
            tool_id=spec.id,
            profile_id=profile_id,
            run_id=get_source_id(),
        )
        return _timeout_tool_error_payload(spec, timeout)
    except Exception:
        record_tool_failure(
            tool_id=spec.id,
            profile_id=profile_id,
            run_id=get_source_id(),
        )
        raise
    return result if isinstance(result, str) else str(result)


def _finalize_tool_result(
    tool_name: str,
    result: Any,
    usage_records: list[dict],
) -> tuple[str, dict[str, Any]]:
    content, artifact = _tool_content_and_artifact(result, usage_records, tool_name=tool_name)
    token_usage = artifact.get("token_usage")
    if not isinstance(token_usage, dict) or not token_usage:
        scope = get_active_tool_scope()
        if scope is not None:
            token_usage = scope.breakdown()
            if token_usage:
                artifact["token_usage"] = token_usage
    if isinstance(token_usage, dict) and token_usage:
        publish_pending_tool_token_usage(tool_name, token_usage)
    return content, artifact


def _tool_content_and_artifact(
    result: Any,
    usage_records: list[dict],
    *,
    tool_name: str = "",
) -> tuple[str, dict[str, Any]]:
    content = result if isinstance(result, str) else str(result)
    usage_summary = structured_llm_usage_summary(usage_records)
    artifact: dict[str, Any] = {}
    usage = usage_summary.get("usage")
    calls = usage_summary.get("calls")
    token_usage = usage_summary.get("token_usage")
    if _has_token_usage(usage):
        artifact["internal_model_usage"] = usage
    if isinstance(calls, list) and calls:
        artifact["internal_model_calls"] = calls
    # token_usage (totals + by_model) is written into the Langfuse TOOL
    # observation metadata on_tool_end so the tool lifecycle can be audited.
    if isinstance(token_usage, dict) and token_usage:
        artifact["token_usage"] = token_usage
    raw = result if isinstance(result, (str, dict, ToolResult)) else content
    try:
        artifact["tool_result"] = summarize_tool_result(tool_name, raw)
    except Exception:
        logger.opt(exception=True).debug(
            "[langfuse] failed to summarize tool_result for {}",
            tool_name or "unknown",
        )
    return content, artifact


def _has_token_usage(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(value.get(field), int) and value.get(field) > 0
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


async def _invoke_with_timeout(
    invocation: Any,
    spec: ToolSpec,
    *,
    profile_id: str | None = None,
) -> str:
    if spec.execution_timeout_seconds is None:
        try:
            return await invocation
        except Exception:
            record_tool_failure(
                tool_id=spec.id,
                profile_id=profile_id,
                run_id=get_source_id(),
            )
            raise
    try:
        return await asyncio.wait_for(invocation, timeout=spec.execution_timeout_seconds)
    except TimeoutError:
        record_tool_timeout(
            tool_id=spec.id,
            profile_id=profile_id,
            run_id=get_source_id(),
        )
        return _timeout_tool_error_payload(spec, spec.execution_timeout_seconds)
    except Exception:
        record_tool_failure(
            tool_id=spec.id,
            profile_id=profile_id,
            run_id=get_source_id(),
        )
        raise


def _timeout_tool_error_payload(spec: ToolSpec, timeout_seconds: float) -> str:
    message = f"{spec.name} timed out after {timeout_seconds:g} seconds"
    _log_tool_timeout(spec, timeout_seconds)
    return ToolResult.fail(
        spec.name,
        ToolErrorCode.TIMEOUT,
        message,
        detail=message,
    ).to_json()


def _queue_timeout_tool_error_payload(spec: ToolSpec, message: str) -> str:
    return ToolResult.fail(
        spec.name,
        ToolErrorCode.QUEUE_TIMEOUT,
        message,
        detail=message,
        retryable=True,
    ).to_json()


def _budget_cancelled_tool_error_payload(
    spec: ToolSpec,
    *,
    dimensions: tuple[str, ...] | list[str] | None = None,
) -> str:
    dims = tuple(dimensions or ())
    reasons: list[str] = []
    if "recursion_steps" in dims:
        reasons.append("图执行步数已达运行预算上限")
    if "elapsed_time" in dims:
        reasons.append("运行 wall-clock 时间已达预算上限")
    if not reasons:
        reasons.append("运行预算已耗尽")
    reason = "；".join(reasons)
    message = (
        f"{spec.name} 未执行：{reason}。"
        "本 run 的工具调用已停止，后续工具调用也不会被执行。"
        "不要再调用任何工具；请说明运行预算已耗尽，"
        "基于已有信息回答用户问题，并引导用户继续提问。"
    )
    logger.bind(tool_id=spec.id, tool_name=spec.name, dimensions=list(dims)).warning(
        "tool cancelled by run budget exhaustion"
    )
    return ToolResult.fail(
        spec.name,
        ToolErrorCode.BUDGET_CANCELLED,
        message,
        detail=message,
        data={"dimensions": list(dims)} if dims else None,
    ).to_json()


def _log_tool_timeout(spec: ToolSpec, timeout_seconds: float) -> None:
    logger.bind(
        tool_id=spec.id,
        tool_name=spec.name,
        tool_timeout_seconds=timeout_seconds,
        tool_resource_class=spec.resource_class or "",
    ).warning("tool execution timed out")
