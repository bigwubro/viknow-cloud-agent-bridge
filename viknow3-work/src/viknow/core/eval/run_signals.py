"""将一次运行的可观测事件投影为稳定、无内容的评测信号。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from viknow.core.answer_review.domain import STOP_GATE_CHECK_TOOL_NAME
from viknow.core.runtime.events import AgentEvent, AgentEventType
from viknow.core.runtime.evidence_ledger import build_run_evidence_ledger


class RunSignal(BaseModel):
    """一个可追溯但不包含用户内容的运行信号。"""

    kind: str
    severity: Literal["info", "warning"]
    event_sequences: list[int]
    attributes: dict[str, int | str | bool] = Field(default_factory=dict)


class RunSignalProjection(BaseModel):
    """一次运行的确定性信号投影。"""

    schema_version: int = 1
    run_id: str
    terminal_status: Literal["completed", "failed", "interrupted", "incomplete"]
    final_answer_event_sequence: int | None = None
    signals: list[RunSignal] = Field(default_factory=list)


_TERMINAL_EVENT_TYPES = {
    AgentEventType.RUN_COMPLETED,
    AgentEventType.RUN_FAILED,
    AgentEventType.RUN_INTERRUPTED,
}


def project_run_signals(events: Iterable[AgentEvent]) -> RunSignalProjection:
    """从同一 run 的事件重建用于离线评测的基础信号。

    该层只表达可由运行事实确定的信号；不推断答案质量，也不携带回答、
    prompt 或工具原始结果。需要语义判断的部分由后续评测技能或模型处理。
    """

    run_events = list(events)
    if not run_events:
        raise ValueError("至少需要一个运行事件来投影信号")

    run_id = run_events[0].run_id
    if any(event.run_id != run_id for event in run_events):
        raise ValueError("运行信号投影只能处理同一个 run 的事件")

    ledger = build_run_evidence_ledger(run_events)
    signals: list[RunSignal] = []
    terminal_event = next(
        (event for event in reversed(run_events) if event.type in _TERMINAL_EVENT_TYPES),
        None,
    )
    terminal_status = (
        terminal_event.type.value.removeprefix("run_")
        if terminal_event is not None
        else "incomplete"
    )
    final_answer_event = next(
        (event for event in reversed(run_events) if event.type == AgentEventType.MESSAGE_COMPLETED),
        None,
    )
    run_started = next(
        (event for event in run_events if event.type == AgentEventType.RUN_STARTED),
        None,
    )
    behavior_snapshot = (
        (run_started.payload or {}).get("behavior_snapshot") if run_started else None
    )

    if not isinstance(behavior_snapshot, dict):
        source_event = run_started or terminal_event or run_events[0]
        signals.append(
            RunSignal(
                kind="behavior_snapshot_missing",
                severity="warning",
                event_sequences=[source_event.sequence],
            )
        )

    _append_stop_gate_signals(signals, run_events)
    _append_tool_gate_signals(signals, run_events)
    _append_candidate_ambiguity_signals(signals, ledger, run_started)

    unlinked_candidates = [
        manifest
        for manifest in ledger.candidate_manifests
        if not any(
            _locator_matches(candidate_locator, claim.locator)
            for candidate_locator in manifest.locators
            for claim in ledger.claims
            if claim.locator is not None
        )
    ]
    if unlinked_candidates:
        signals.append(
            RunSignal(
                kind="candidate_not_inspected",
                severity="info",
                event_sequences=[
                    manifest.source_event_sequence for manifest in unlinked_candidates
                ],
                attributes={"unlinked_candidate_count": len(unlinked_candidates)},
            )
        )

    verified_claim_sequences = [
        claim.source_event_sequence for claim in ledger.claims if claim.status.value == "verified"
    ]
    if verified_claim_sequences:
        signals.append(
            RunSignal(
                kind="verified_claim_available",
                severity="info",
                event_sequences=verified_claim_sequences,
                attributes={"claim_count": len(verified_claim_sequences)},
            )
        )

    for category in {incident.category for incident in ledger.incidents}:
        incident_sequences = [
            incident.source_event_sequence
            for incident in ledger.incidents
            if incident.category == category
        ]
        signals.append(
            RunSignal(
                kind=f"tool_{category.value}",
                severity="warning",
                event_sequences=incident_sequences,
                attributes={"incident_count": len(incident_sequences)},
            )
        )

    for review in ledger.reviews:
        if review.retryable and not review.passed and review.attempt < review.max_attempts:
            signals.append(
                RunSignal(
                    kind="answer_review_retry_started",
                    severity="info",
                    event_sequences=[review.source_event_sequence],
                    attributes={"attempt": review.attempt, "max_attempts": review.max_attempts},
                )
            )
    if ledger.reviews:
        final_review = ledger.reviews[-1]
        attributes: dict[str, int | str | bool] = {
            "attempt": final_review.attempt,
            "max_attempts": final_review.max_attempts,
        }
        if final_review.overall_score is not None:
            attributes["overall_score"] = final_review.overall_score
        signals.append(
            RunSignal(
                kind=("answer_review_passed" if final_review.passed else "answer_review_failed"),
                severity="info" if final_review.passed else "warning",
                event_sequences=[final_review.source_event_sequence],
                attributes=attributes,
            )
        )

    budget_exhausted = [
        incident for incident in ledger.incidents if incident.category.value == "budget_exhausted"
    ]
    if budget_exhausted:
        signals.append(
            RunSignal(
                kind="run_budget_exhausted",
                severity="warning",
                event_sequences=[item.source_event_sequence for item in budget_exhausted],
                attributes={"count": len(budget_exhausted)},
            )
        )

    if ledger.compactions:
        signals.append(
            RunSignal(
                kind="context_compacted",
                severity="info",
                event_sequences=[item.source_event_sequence for item in ledger.compactions],
                attributes={"count": len(ledger.compactions)},
            )
        )

    if final_answer_event is not None:
        citations = (final_answer_event.payload or {}).get("citations")
        if verified_claim_sequences and not citations:
            signals.append(
                RunSignal(
                    kind="final_answer_without_citation",
                    severity="warning",
                    event_sequences=[final_answer_event.sequence],
                )
            )
    elif terminal_status in {"completed", "failed"}:
        source_event = terminal_event or run_events[-1]
        signals.append(
            RunSignal(
                kind="final_answer_missing",
                severity="warning",
                event_sequences=[source_event.sequence],
            )
        )

    return RunSignalProjection(
        run_id=run_id,
        terminal_status=terminal_status,
        final_answer_event_sequence=final_answer_event.sequence if final_answer_event else None,
        signals=signals,
    )


def _append_stop_gate_signals(signals: list[RunSignal], events: list[AgentEvent]) -> None:
    for index, event in enumerate(events):
        if event.type != AgentEventType.STOP_GATE_EVALUATED or not bool(
            event.payload.get("blocked")
        ):
            continue
        reason_code = str(event.payload.get("reason_code") or "")
        attributes: dict[str, int | str | bool] = {"reason_code": reason_code}
        next_action = event.payload.get("next_action")
        if isinstance(next_action, str) and next_action:
            attributes["next_action"] = next_action
        signals.append(
            RunSignal(
                kind="stop_gate_blocked",
                severity="warning",
                event_sequences=[event.sequence],
                attributes=attributes,
            )
        )
        if reason_code == "uninspected_knowledge_evidence":
            signals.append(
                RunSignal(
                    kind="stop_gate_uninspected_knowledge_evidence",
                    severity="warning",
                    event_sequences=[event.sequence],
                )
            )
        evidence_tools = event.payload.get("available_evidence_tools")
        if isinstance(evidence_tools, list):
            for tool_name in sorted({str(tool) for tool in evidence_tools if str(tool)}):
                signals.append(
                    RunSignal(
                        kind="stop_gate_evidence_tool_available",
                        severity="info",
                        event_sequences=[event.sequence],
                        attributes={"tool_name": tool_name},
                    )
                )
        continuation = _following_stop_gate_check(events[index + 1 :])
        if continuation is not None:
            signals.append(
                RunSignal(
                    kind="stop_gate_continued",
                    severity="info",
                    event_sequences=[event.sequence, continuation.sequence],
                )
            )
        outcome = _following_stop_gate_outcome(events[index + 1 :], reason_code)
        if outcome is None:
            continue
        outcome_kind, outcome_event = outcome
        signals.append(
            RunSignal(
                kind=outcome_kind,
                severity="warning" if outcome_kind == "stop_gate_released" else "info",
                event_sequences=[event.sequence, outcome_event.sequence],
            )
        )


def _following_stop_gate_check(events: list[AgentEvent]) -> AgentEvent | None:
    for event in events:
        if event.type == AgentEventType.STOP_GATE_EVALUATED:
            return None
        if event.type == AgentEventType.STOP_GATE_RELEASED:
            return None
        if event.type != AgentEventType.TOOL_CALL_COMPLETED:
            continue
        if str(event.payload.get("name") or "") == STOP_GATE_CHECK_TOOL_NAME:
            return event
    return None


def _following_stop_gate_outcome(
    events: list[AgentEvent], reason_code: str
) -> tuple[str, AgentEvent] | None:
    for event in events:
        if event.type == AgentEventType.STOP_GATE_EVALUATED:
            if not bool(event.payload.get("blocked")):
                return "stop_gate_recovered", event
            continue
        if event.type != AgentEventType.STOP_GATE_RELEASED:
            continue
        release_reason = str(event.payload.get("reason_code") or "")
        if not release_reason or release_reason == reason_code:
            return "stop_gate_released", event
    return None


def _append_candidate_ambiguity_signals(
    signals: list[RunSignal],
    ledger,
    run_started: AgentEvent | None,
) -> None:
    selected_assets = _selected_asset_keys(run_started)
    candidate_locators = [
        (manifest.source_event_sequence, locator)
        for manifest in ledger.candidate_manifests
        for locator in manifest.locators
        if locator.asset_id
    ]
    asset_keys = {(locator.library_id, locator.asset_id) for _, locator in candidate_locators}
    if not selected_assets and len(asset_keys) > 1:
        signals.append(
            RunSignal(
                kind="source_ambiguous",
                severity="warning",
                event_sequences=sorted({sequence for sequence, _ in candidate_locators}),
                attributes={"asset_count": len(asset_keys)},
            )
        )

    video_occurrences: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for sequence, locator in candidate_locators:
        if locator.modality != "video" or locator.time_range is None:
            continue
        start = locator.time_range["start_seconds"]
        end = locator.time_range["end_seconds"]
        video_occurrences.setdefault((locator.library_id, locator.asset_id), []).append(
            (sequence, start, end)
        )
    for (_, asset_id), occurrences in sorted(video_occurrences.items()):
        distinct_occurrences = _distinct_video_occurrences(occurrences)
        if len(distinct_occurrences) < 2:
            continue
        signals.append(
            RunSignal(
                kind="multiple_video_occurrences_exposed",
                severity="info",
                event_sequences=sorted({sequence for sequence, _, _ in occurrences}),
                attributes={"asset_id": asset_id, "occurrence_count": len(distinct_occurrences)},
            )
        )


def _selected_asset_keys(run_started: AgentEvent | None) -> set[tuple[str, str]]:
    if run_started is None:
        return set()
    payload = run_started.payload
    selected = payload.get("selected_knowledge_assets")
    if not isinstance(selected, list):
        behavior_snapshot = payload.get("behavior_snapshot")
        knowledge_start = (
            behavior_snapshot.get("knowledge_start_state")
            if isinstance(behavior_snapshot, dict)
            else None
        )
        selected = (
            knowledge_start.get("selected_knowledge_assets")
            if isinstance(knowledge_start, dict)
            else []
        )
    if not isinstance(selected, list):
        selected = []
    return {
        (str(asset.get("library_id") or ""), str(asset.get("asset_id") or ""))
        for asset in selected
        if isinstance(asset, dict) and asset.get("asset_id")
    }


def _distinct_video_occurrences(
    occurrences: list[tuple[int, float, float]],
) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for _, start, end in sorted(occurrences, key=lambda item: (item[1], item[2])):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _append_tool_gate_signals(signals: list[RunSignal], events: list[AgentEvent]) -> None:
    for event in events:
        if event.type != AgentEventType.TOOL_GATE_EVALUATED or not event.payload.get("blocked"):
            continue
        signals.append(
            RunSignal(
                kind="tool_gate_blocked",
                severity="info",
                event_sequences=[event.sequence],
                attributes={
                    "tool_name": str(event.payload.get("tool_name") or ""),
                    "reason_code": str(event.payload.get("reason_code") or ""),
                },
            )
        )


def _locator_matches(candidate_locator, claim_locator) -> bool:
    """Return whether two content-free locators identify overlapping evidence."""

    if (
        candidate_locator.library_id
        and claim_locator.library_id
        and candidate_locator.library_id != claim_locator.library_id
    ):
        return False
    if (
        candidate_locator.asset_id
        and claim_locator.asset_id
        and candidate_locator.asset_id != claim_locator.asset_id
    ):
        return False
    if (
        candidate_locator.segment_id
        and claim_locator.segment_id
        and candidate_locator.segment_id != claim_locator.segment_id
    ):
        return False
    candidate_range = candidate_locator.time_range
    claim_range = claim_locator.time_range
    if candidate_range is not None and claim_range is not None:
        if candidate_range["end_seconds"] < claim_range["start_seconds"]:
            return False
        if claim_range["end_seconds"] < candidate_range["start_seconds"]:
            return False
    return bool(
        (candidate_locator.asset_id and claim_locator.asset_id)
        or (candidate_locator.segment_id and claim_locator.segment_id)
        or (candidate_range is not None and claim_range is not None)
    )
