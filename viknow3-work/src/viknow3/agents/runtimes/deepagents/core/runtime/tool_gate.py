"""Deterministic pre-execution checks for knowledge-QA tool arguments."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from viknow3.agents.runtimes.deepagents.core.answer_review.claim_bundle import VIDEO_INSPECT_TOOL_NAMES
from viknow3.agents.runtimes.deepagents.core.runtime.domain import ToolCallRecord
from viknow3.agents.runtimes.deepagents.core.runtime.evidence_ledger import RunEvidenceLedger
from viknow3.agents.runtimes.deepagents.core.tools.base import ToolErrorCode, nested_tool_failure, unwrap_tool_data

_INSPECT_TOOL_NAMES = frozenset(
    {
        "text_inspect",
        "image_inspect",
        "document_inspect",
        "visual_ground",
        *VIDEO_INSPECT_TOOL_NAMES,
    }
)
_LISTED_VIDEO_INSPECT_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class ToolGateDecision:
    allowed: bool
    reason_code: str = ""
    next_action: str = ""
    recovery: ToolGateRecovery | None = None


@dataclass(frozen=True)
class ToolGateRecovery:
    """A fact constraint the Agent must satisfy after a rejected tool call."""

    requirement_code: str
    constraint: str
    available_values: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "requirement_code": self.requirement_code,
            "constraint": self.constraint,
            "available_values": list(self.available_values),
        }


@dataclass
class ToolGateContext:
    """Run facts available before one tool is allowed to execute."""

    selected_library_ids: set[str] = field(default_factory=set)
    selected_asset_ids: set[str] = field(default_factory=set)
    enforce_selected_asset_scope: bool = False
    oracle_collection: bool = False
    completed_tool_calls: list[ToolCallRecord] = field(default_factory=list)
    ledger: RunEvidenceLedger | None = None
    # Verified candidate locators from earlier runs in the same session. This
    # is deliberately separate from ``ledger`` so run-local budgets and
    # duplicate-call guards do not leak across turns.
    session_ledger: RunEvidenceLedger | None = None
    budget_warning: bool = False
    targeted_repair: bool = False


def evaluate_tool_gate(
    context: ToolGateContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolGateDecision:
    """Allow only calls whose deterministic prerequisites are present in this run."""

    asset_scope = _asset_scope_rejection(context, tool_name=tool_name, arguments=arguments)
    if asset_scope is not None:
        return asset_scope
    oracle_budget = _oracle_collection_budget_rejection(
        context,
        tool_name=tool_name,
        arguments=arguments,
    )
    if oracle_budget is not None:
        return oracle_budget
    if context.targeted_repair and tool_name in {
        "knowledge_discover",
        "knowledge_search",
        "query_refine",
        "task",
    }:
        return ToolGateDecision(
            allowed=False,
            reason_code="targeted_repair_no_broad_search",
            next_action="Only inspect the identified missing evidence or use list_video_segments "
            "for legal adjacent ranges. Reuse existing claims; do not restart the search chain.",
        )
    if (
        tool_name == "knowledge_search"
        and context.budget_warning
        and _has_uninspected_candidates(context)
    ):
        recovery = _inspect_locator_recovery(context, arguments)
        return ToolGateDecision(
            allowed=False,
            reason_code="budget_prioritize_uninspected_candidate",
            next_action="Inspect a decisive existing candidate before further broad search.",
            recovery=recovery,
        )
    if tool_name == "knowledge_search":
        library_id = str(arguments.get("library_id") or "")
        library_ids = _run_library_ids(context)
        if not library_id or library_id not in library_ids:
            recovery = _library_scope_recovery(library_ids)
            return ToolGateDecision(
                allowed=False,
                reason_code="library_not_in_run_scope",
                next_action=recovery.constraint,
                recovery=recovery,
            )
    if tool_name in _INSPECT_TOOL_NAMES:
        inspection_rejection = _inspect_locator_rejection(
            context,
            arguments,
            enforce_listed_video_limit=tool_name in VIDEO_INSPECT_TOOL_NAMES,
        )
        if inspection_rejection is not None:
            return inspection_rejection
    repeated = _repeated_call_decision(context, tool_name, arguments)
    if repeated is not None:
        return repeated
    return ToolGateDecision(allowed=True)


def _oracle_collection_budget_rejection(
    context: ToolGateContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolGateDecision | None:
    """Keep Oracle cataloging bounded without constraining ordinary QA."""

    if not context.oracle_collection:
        return None
    if tool_name == "knowledge_search":
        recovery = ToolGateRecovery(
            requirement_code="oracle_catalog_without_search",
            constraint=(
                "本 Oracle 已有 Gate A 批准的唯一范围；使用 knowledge_discover 验证范围后，"
                "改用 list_video_segments 或 list_doc_segments 的 map/select 获取 locator。"
            ),
        )
        return ToolGateDecision(
            allowed=False,
            reason_code="oracle_knowledge_search_not_allowed",
            next_action=recovery.constraint,
            recovery=recovery,
        )
    if tool_name not in {"list_video_segments", "list_doc_segments"}:
        return None
    if not any(call.name == "evidence_plan" for call in context.completed_tool_calls):
        recovery = ToolGateRecovery(
            requirement_code="oracle_evidence_plan_completed",
            constraint=(
                "目录建图必须基于已完成的 EvidencePlan；先完成轻量 scope discovery，"
                "由主 Agent 调用 evidence_plan，再按其中的 subquestion 使用 map/select。"
            ),
        )
        return ToolGateDecision(
            allowed=False,
            reason_code="oracle_evidence_plan_required",
            next_action=recovery.constraint,
            recovery=recovery,
        )
    # A catalog page is metadata, not an inspect result. Research may need to
    # traverse additional pages after a coverage gap; duplicate pages remain
    # blocked by the common repeated-call guard.
    return None


def _asset_scope_rejection(
    context: ToolGateContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolGateDecision | None:
    if not context.enforce_selected_asset_scope:
        return None
    if context.oracle_collection and not context.selected_asset_ids:
        return None
    if tool_name not in _INSPECT_TOOL_NAMES | {"list_video_segments", "list_doc_segments"}:
        return None
    asset_id = str(arguments.get("asset_id") or arguments.get("document_id") or "")
    if asset_id and asset_id in context.selected_asset_ids:
        return None
    recovery = ToolGateRecovery(
        requirement_code="asset_id_in_run_scope",
        constraint="后续工具调用的 asset_id 必须来自本次已批准的知识资产范围。",
        available_values=tuple(sorted(context.selected_asset_ids)),
    )
    return ToolGateDecision(
        allowed=False,
        reason_code="asset_not_in_run_scope",
        next_action=recovery.constraint,
        recovery=recovery,
    )


def _library_scope_recovery(library_ids: set[str]) -> ToolGateRecovery:
    available_values = tuple(sorted(library_ids))
    if available_values:
        constraint = (
            "后续 knowledge_search 的 library_id 必须来自本轮或当前会话已明确的知识库范围；"
            "可复用已有结果，无需重复获取范围。"
        )
    else:
        constraint = (
            "后续 knowledge_search 前必须取得本轮或当前会话的知识库范围；"
            "该范围可来自用户明确选择或 knowledge_discover 的结果。"
        )
    return ToolGateRecovery(
        requirement_code="library_id_in_run_scope",
        constraint=constraint,
        available_values=available_values,
    )


def _inspect_locator_recovery(
    context: ToolGateContext, arguments: dict[str, Any] | None = None
) -> ToolGateRecovery:
    return ToolGateRecovery(
        requirement_code="inspect_locator_in_run_candidates",
        constraint=(
            "后续检查调用的 library_id、asset_id、segment_id 和时间范围必须引用"
            "本轮或当前会话已获得的候选定位；复制 available_values 中的合法参数。"
            "跨边界需求先用 list_video_segments 取得相邻片段，分别 inspect，禁止合并伪造范围。"
        ),
        available_values=_candidate_locator_values(context, arguments),
    )


def _listed_video_inspect_limit_recovery() -> ToolGateRecovery:
    return ToolGateRecovery(
        requirement_code="listed_video_inspect_max_duration",
        constraint=(
            "由 list_video_segments 得到的连续片段可合并检查，但单次视频核验工具"
            "时间范围不得超过 30 秒；请拆分为连续的短时间窗。"
        ),
    )


def _run_library_ids(context: ToolGateContext) -> set[str]:
    library_ids = set(context.selected_library_ids)
    for call in context.completed_tool_calls:
        _add_discovered_library_ids(library_ids, call)
    if context.session_ledger is not None:
        for manifest in context.session_ledger.candidate_manifests:
            library_ids.update(
                locator.library_id for locator in manifest.locators if locator.library_id
            )
    return library_ids


def _add_discovered_library_ids(library_ids: set[str], call: ToolCallRecord) -> None:
    if call.name != "knowledge_discover":
        return
    result = unwrap_tool_data(_json_object(call.result) or {})
    libraries = result.get("libraries") if result else None
    if not isinstance(libraries, list):
        return
    library_ids.update(
        str(library.get("id") or "")
        for library in libraries
        if isinstance(library, dict) and library.get("id")
    )


def _inspect_locator_rejection(
    context: ToolGateContext,
    arguments: dict[str, Any],
    *,
    enforce_listed_video_limit: bool,
) -> ToolGateDecision | None:
    if context.ledger is None and context.session_ledger is None:
        return _unknown_inspect_locator_decision(context)
    library_id = str(arguments.get("library_id") or "")
    asset_id = str(arguments.get("asset_id") or "")
    segment_id = str(arguments.get("evidence_id") or arguments.get("segment_id") or "")
    time_range = _time_range(arguments.get("time_range"))
    if arguments.get("time_range") is not None and time_range is None:
        return _unknown_inspect_locator_decision(context)
    ledgers = tuple(
        ledger for ledger in (context.ledger, context.session_ledger) if ledger is not None
    )
    for ledger in ledgers:
        for manifest in ledger.candidate_manifests:
            if manifest.tool_name == "list_video_segments":
                continue
            for locator in manifest.locators:
                if library_id and locator.library_id != library_id:
                    continue
                if asset_id and locator.asset_id != asset_id:
                    continue
                if segment_id and locator.segment_id != segment_id:
                    continue
                if time_range is not None and not _range_within(time_range, locator.time_range):
                    continue
                if locator.asset_id or locator.segment_id or locator.time_range is not None:
                    return None
    if _within_listed_video_coverage(context, library_id, asset_id, time_range):
        if (
            not enforce_listed_video_limit
            or time_range is None
            or time_range[1] - time_range[0] <= _LISTED_VIDEO_INSPECT_MAX_SECONDS
        ):
            return None
        recovery = _listed_video_inspect_limit_recovery()
        return ToolGateDecision(
            allowed=False,
            reason_code="inspect_range_exceeds_listed_segment_limit",
            next_action=recovery.constraint,
            recovery=recovery,
        )
    return _unknown_inspect_locator_decision(context)


def _unknown_inspect_locator_decision(context: ToolGateContext) -> ToolGateDecision:
    recovery = _inspect_locator_recovery(context)
    return ToolGateDecision(
        allowed=False,
        reason_code="inspect_locator_not_in_run_candidates",
        next_action=recovery.constraint,
        recovery=recovery,
    )


def _within_listed_video_coverage(
    context: ToolGateContext,
    library_id: str,
    asset_id: str,
    requested_range: tuple[float, float] | None,
) -> bool:
    if requested_range is None or not library_id or not asset_id:
        return False
    ledgers = tuple(
        ledger for ledger in (context.ledger, context.session_ledger) if ledger is not None
    )
    intervals = sorted(
        (
            _time_range(locator.time_range)
            for ledger in ledgers
            for manifest in ledger.candidate_manifests
            if manifest.tool_name == "list_video_segments"
            for locator in manifest.locators
            if locator.library_id == library_id and locator.asset_id == asset_id
        ),
        key=lambda interval: interval or (float("inf"), float("inf")),
    )
    coverage: list[tuple[float, float]] = []
    for interval in intervals:
        if interval is None:
            continue
        if coverage and interval[0] <= coverage[-1][1]:
            coverage[-1] = (coverage[-1][0], max(coverage[-1][1], interval[1]))
        else:
            coverage.append(interval)
    return any(start <= requested_range[0] and requested_range[1] <= end for start, end in coverage)


def _candidate_locator_values(
    context: ToolGateContext, arguments: dict[str, Any] | None = None
) -> tuple[str, ...]:
    ledgers = tuple(
        ledger for ledger in (context.ledger, context.session_ledger) if ledger is not None
    )
    if not ledgers:
        return ()
    values: list[str] = []
    for ledger in ledgers:
        for manifest in ledger.candidate_manifests:
            for locator in manifest.locators:
                value = json.dumps(
                    {
                        "library_id": locator.library_id,
                        "asset_id": locator.asset_id,
                        "evidence_id": locator.segment_id,
                        "time_range": locator.time_range,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if value not in values:
                    values.append(value)
    asset_id = (arguments or {}).get("asset_id")
    values.sort(key=lambda value: json.loads(value).get("asset_id") != asset_id)
    return tuple(values[:20])


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _repeated_call_decision(
    context: ToolGateContext,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolGateDecision | None:
    # Run-state operations cannot be deduplicated by argument identity. Keep
    # answer_plan outside this generic policy until harness policies model
    # state-dependent reuse; the tool still validates inputs and current facts.
    if tool_name == "answer_plan":
        return None

    def execution_arguments(value):
        # Model-side progress is not part of the independently callable tool.
        if tool_name in _INSPECT_TOOL_NAMES | {"knowledge_search", "list_video_segments"}:
            return {key: item for key, item in value.items() if key != "run_progress"}
        return value

    normalized_arguments = _normalized_json(execution_arguments(arguments))
    for call in context.completed_tool_calls:
        call_arguments = _normalized_json(execution_arguments(_json_object(call.arguments) or {}))
        if call.name != tool_name or call_arguments != normalized_arguments:
            continue
        if (
            tool_name == "submit_run_analysis"
            and (_json_object(call.result) or {}).get("accepted") is False
        ):
            # A rejected draft is not a completed submission; the file can be
            # corrected while its path (and therefore tool arguments) stays fixed.
            continue
        error_code = _error_code(call.result)
        if error_code:
            # Rejected calls never reached execution. Current prerequisites have
            # already been checked above; a rejection is not a failed attempt.
            if error_code == ToolErrorCode.GATE_BLOCKED:
                continue
            payload = _json_object(call.result) or {}
            error = payload.get("error")
            nested = nested_tool_failure(payload)
            if not error and nested is not None and nested["retryable"] is not False:
                # Unknown retryability is not success. Run-owned execution and
                # original scheduler budgets still bound another real attempt.
                continue
            # This permits a new Agent request, not an automatic retry loop.
            # Existing run/scheduler budgets still govern execution.
            if isinstance(error, dict) and error.get("retryable") is True:
                continue
            recovery = ToolGateRecovery(
                requirement_code="changed_tool_attempt",
                constraint="同参数调用已有不可重试的执行失败；请依据原始错误修正调用，或说明当前限制。",
            )
            return ToolGateDecision(
                allowed=False,
                reason_code="repeat_failed_tool_call",
                next_action=recovery.constraint,
                recovery=recovery,
            )
        recovery = ToolGateRecovery(
            requirement_code="reuse_completed_result",
            constraint="应复用本 run 已完成的同一工具结果，而非重复调用。",
        )
        return ToolGateDecision(
            allowed=False,
            reason_code="duplicate_successful_tool_call",
            next_action=recovery.constraint,
            recovery=recovery,
        )
    return None


def _normalized_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error_code(value: Any) -> str:
    result = _json_object(value)
    error = result.get("error") if result is not None else None
    if isinstance(error, dict):
        return str(error.get("code") or "tool_execution_error")
    if isinstance(error, str) and error.strip():
        return "tool_execution_error"
    nested = nested_tool_failure(result)
    return nested["code"] if nested is not None else ""


def _time_range(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    start = value.get("start_seconds")
    end = value.get("end_seconds")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not all(math.isfinite(v) for v in (start, end)) or start >= end:
        return None
    return float(start), float(end)


def _range_within(requested: tuple[float, float], candidate: dict[str, float] | None) -> bool:
    if candidate is None:
        return False
    return requested[0] >= candidate["start_seconds"] and requested[1] <= candidate["end_seconds"]


def _has_uninspected_candidates(context: ToolGateContext) -> bool:
    if context.ledger is None:
        return False
    for manifest in context.ledger.candidate_manifests:
        for locator in manifest.locators:
            if not locator.asset_id or not locator.time_range:
                continue
            inspected = False
            for call in context.completed_tool_calls:
                if call.name not in _INSPECT_TOOL_NAMES or _error_code(call.result):
                    continue
                args = _json_object(call.arguments) or {}
                if args.get("asset_id") != locator.asset_id:
                    continue
                interval = _time_range(args.get("time_range"))
                if interval and _range_within(interval, locator.time_range):
                    inspected = True
                    break
            if not inspected:
                return True
    return False
