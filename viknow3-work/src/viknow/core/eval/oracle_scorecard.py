"""Trace-derived scorecard for Evidence Oracle research.

The scorecard deliberately separates runtime workflow facts from optional,
offline-only annotation comparison.  It never supplies annotation content to
an Oracle run and it never turns structural completion into accepted evidence.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.answer_review.claim_bundle import VIDEO_INSPECT_TOOL_NAMES
from viknow.core.eval.trace import EvalRunTrace


class OracleTimeWindow(BaseModel):
    """One evaluation-only time window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float
    end_seconds: float

    @model_validator(mode="after")
    def validate_range(self) -> OracleTimeWindow:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("time window end_seconds must be greater than start_seconds")
        return self


class OracleEvaluationAnnotation(BaseModel):
    """Offline reference windows; never part of the Oracle runtime contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    expected_time_windows: list[OracleTimeWindow] = Field(min_length=1)


class OraclePlanningScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_present: bool


class OracleCatalogScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_count: int = Field(ge=0)
    failed_report_count: int = Field(ge=0)
    partition_complete: bool
    unclassified_segment_ids: list[str] = Field(default_factory=list)
    duplicate_segment_ids: list[str] = Field(default_factory=list)
    unknown_segment_ids: list[str] = Field(default_factory=list)


class OracleEventMapScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    present: bool
    event_count: int = Field(ge=0)


class OracleInspectionScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inspector_task_count: int = Field(ge=0)
    inspect_call_count: int = Field(ge=0)


class OracleChallengeScore(BaseModel):
    """Whether the main agent accounted for Scout's material exclusions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    material_exclusion_count: int = Field(ge=0)
    reviewed_material_exclusion_count: int = Field(ge=0)
    reopened_frontier_ids: list[str] = Field(default_factory=list)
    reopened_frontier_ids_mapped: bool


class OracleReflectionScore(BaseModel):
    """Trace facts showing whether a later search round followed reflection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    present: bool
    targeted_research_task_count: int = Field(ge=0)
    strategy_adjusted: bool


class OracleAnnotationComparison(BaseModel):
    """Post-run comparison only; these values are not runtime quality claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    candidate_time_recall: float = Field(ge=0.0, le=1.0)
    event_time_recall: float = Field(ge=0.0, le=1.0)


class OracleResearchScorecard(BaseModel):
    """Auditable workflow and optional offline coverage view for one Oracle trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    planning: OraclePlanningScore
    catalog: OracleCatalogScore
    event_map: OracleEventMapScore
    inspection: OracleInspectionScore
    challenge: OracleChallengeScore
    reflection: OracleReflectionScore
    evaluation: OracleAnnotationComparison | None = None


def build_oracle_research_scorecard(
    trace: EvalRunTrace,
    *,
    annotation: OracleEvaluationAnnotation | None = None,
) -> OracleResearchScorecard:
    """Project Oracle workflow facts from an already-completed eval trace."""

    raw_calls = trace.metadata.tool_call_records
    failed_call_ids = _failed_tool_call_ids(trace)
    calls = [
        call
        for call in raw_calls
        if call.get("status") != "failed"
        and str(call.get("id") or "") not in failed_call_ids
        and not _tool_result_is_error(call.get("result"))
    ]
    names = [str(call.get("name") or "") for call in calls]
    reports = [_scout_report(call) for call in calls if call.get("name") == "evidence_scout_report"]
    reports = [report for report in reports if report is not None]
    catalog = _catalog_score(
        reports,
        failed_report_count=sum(
            1
            for call in raw_calls
            if call.get("name") == "evidence_scout_report"
            and (
                call.get("status") == "failed"
                or str(call.get("id") or "") in failed_call_ids
                or _tool_result_is_error(call.get("result"))
            )
        ),
    )
    events = _event_windows(calls)
    mapped_frontier_ids = _event_frontier_ids(calls)
    challenge = _challenge_score(calls, reports, mapped_frontier_ids)
    reflection = _reflection_score(calls)
    candidate_windows = _candidate_windows(reports)
    evaluation = None
    if annotation is not None:
        evaluation = OracleAnnotationComparison(
            case_id=annotation.case_id,
            candidate_time_recall=_time_recall(
                annotation.expected_time_windows,
                candidate_windows,
            ),
            event_time_recall=_time_recall(annotation.expected_time_windows, events),
        )
    return OracleResearchScorecard(
        planning=OraclePlanningScore(plan_present="evidence_plan" in names),
        catalog=catalog,
        event_map=OracleEventMapScore(
            present="evidence_event_map" in names,
            event_count=len(events),
        ),
        inspection=OracleInspectionScore(
            inspector_task_count=sum(
                1
                for call in calls
                if call.get("name") == "task" and _is_inspector_task(call.get("arguments"))
            ),
            inspect_call_count=sum(
                1
                for name in names
                if name
                in {
                    *VIDEO_INSPECT_TOOL_NAMES,
                    "text_inspect",
                    "image_inspect",
                    "document_inspect",
                    "visual_ground",
                }
            ),
        ),
        challenge=challenge,
        reflection=reflection,
        evaluation=evaluation,
    )


def _scout_report(call: dict[str, Any]) -> dict[str, Any] | None:
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        return None
    report = arguments.get("scout_report")
    return report if isinstance(report, dict) else None


def _catalog_score(
    reports: list[dict[str, Any]],
    *,
    failed_report_count: int,
) -> OracleCatalogScore:
    unclassified: set[str] = set()
    duplicate: set[str] = set()
    unknown: set[str] = set()
    for report in reports:
        expected = _string_ids(report.get("page_segment_ids"))
        classifications = [
            _string_ids(report.get("candidate_segment_ids")),
            _string_ids(report.get("uncertain_segment_ids")),
            _string_ids(report.get("excluded_segment_ids")),
        ]
        classified = [segment_id for group in classifications for segment_id in group]
        known = set(expected)
        unclassified.update(known - set(classified))
        duplicate.update(
            segment_id for segment_id in set(classified) if classified.count(segment_id) > 1
        )
        unknown.update(set(classified) - known)
    return OracleCatalogScore(
        report_count=len(reports),
        failed_report_count=failed_report_count,
        partition_complete=bool(reports) and not unclassified and not duplicate and not unknown,
        unclassified_segment_ids=sorted(unclassified),
        duplicate_segment_ids=sorted(duplicate),
        unknown_segment_ids=sorted(unknown),
    )


def _failed_tool_call_ids(trace: EvalRunTrace) -> set[str]:
    """Find explicitly failed tool results preserved in the eval context.

    Tool execution can finish normally while the tool returns a structured
    validation error.  ``tool_call_records`` preserves the execution event,
    whereas the paired tool message preserves that result.  The scorecard must
    use both facts so a rejected report never looks like successful research.
    """

    failed_ids: set[str] = set()
    for message in trace.context:
        if not isinstance(message, dict):
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        if not call_id:
            continue
        if any(
            _tool_result_is_error(payload) for payload in _context_payloads(message.get("content"))
        ):
            failed_ids.add(call_id)
    return failed_ids


def _context_payloads(content: object) -> list[object]:
    if isinstance(content, str):
        try:
            return [json.loads(content)]
        except json.JSONDecodeError:
            return [content]
    if isinstance(content, list):
        return [payload for item in content for payload in _context_payloads(item)]
    return [content]


def _tool_result_is_error(result: object) -> bool:
    if isinstance(result, str):
        try:
            return _tool_result_is_error(json.loads(result))
        except json.JSONDecodeError:
            return False
    if not isinstance(result, dict):
        return False
    if result.get("error") not in (None, "", False):
        return True
    tool_gate = result.get("tool_gate")
    return isinstance(tool_gate, dict) and tool_gate.get("blocked") is True


def _event_windows(calls: list[dict[str, Any]]) -> list[OracleTimeWindow]:
    windows: list[OracleTimeWindow] = []
    for call in calls:
        if call.get("name") != "evidence_event_map":
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        events = arguments.get("events")
        if not isinstance(events, list):
            continue
        windows.extend(
            window
            for event in events
            if isinstance(event, dict)
            for window in [_time_window(event.get("time_range"))]
            if window is not None
        )
    return windows


def _candidate_windows(reports: list[dict[str, Any]]) -> list[OracleTimeWindow]:
    windows: list[OracleTimeWindow] = []
    for report in reports:
        locators = report.get("locators")
        if not isinstance(locators, list):
            continue
        windows.extend(
            window
            for locator in locators
            if isinstance(locator, dict)
            for payload in [locator.get("locator")]
            if isinstance(payload, dict)
            for window in [_time_window(payload.get("time_range"))]
            if window is not None
        )
    return windows


def _event_frontier_ids(calls: list[dict[str, Any]]) -> set[str]:
    frontier_ids: set[str] = set()
    for call in calls:
        if call.get("name") != "evidence_event_map":
            continue
        arguments = call.get("arguments")
        events = arguments.get("events") if isinstance(arguments, dict) else None
        if not isinstance(events, list):
            continue
        frontier_ids.update(
            str(frontier_id)
            for event in events
            if isinstance(event, dict)
            for frontier_id in event.get("frontier_ids", [])
            if str(frontier_id).strip()
        )
    return frontier_ids


def _challenge_score(
    calls: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    mapped_frontier_ids: set[str],
) -> OracleChallengeScore:
    material_ids = {
        str(exclusion.get("frontier_id"))
        for report in reports
        for exclusion in report.get("material_exclusions", [])
        if isinstance(exclusion, dict) and str(exclusion.get("frontier_id") or "").strip()
    }
    reviews = [
        review
        for call in calls
        if call.get("name") == "evidence_exclusion_review"
        for review in _exclusion_reviews(call.get("arguments"))
    ]
    reviewed_ids = {
        str(review.get("frontier_id"))
        for review in reviews
        if str(review.get("frontier_id") or "").strip()
    }
    reopened_ids = sorted(
        str(review.get("frontier_id"))
        for review in reviews
        if review.get("main_disposition") == "reopen"
        and str(review.get("frontier_id") or "").strip()
    )
    return OracleChallengeScore(
        material_exclusion_count=len(material_ids),
        reviewed_material_exclusion_count=len(material_ids & reviewed_ids),
        reopened_frontier_ids=reopened_ids,
        reopened_frontier_ids_mapped=set(reopened_ids).issubset(mapped_frontier_ids),
    )


def _exclusion_reviews(arguments: object) -> list[dict[str, Any]]:
    if not isinstance(arguments, dict):
        return []
    review = arguments.get("exclusion_review")
    reviews = review.get("reviews") if isinstance(review, dict) else None
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []


def _reflection_score(calls: list[dict[str, Any]]) -> OracleReflectionScore:
    reflection_present = any(call.get("name") == "evidence_reflection" for call in calls)
    targeted_count = sum(
        1
        for call in calls
        if call.get("name") == "task" and _is_targeted_scout_task(call.get("arguments"))
    )
    return OracleReflectionScore(
        present=reflection_present,
        targeted_research_task_count=targeted_count,
        strategy_adjusted=reflection_present and targeted_count > 0,
    )


def _time_recall(
    expected: list[OracleTimeWindow],
    observed: list[OracleTimeWindow],
) -> float:
    matched = sum(
        any(_windows_overlap(reference, candidate) for candidate in observed)
        for reference in expected
    )
    return matched / len(expected)


def _windows_overlap(left: OracleTimeWindow, right: OracleTimeWindow) -> bool:
    return left.start_seconds < right.end_seconds and right.start_seconds < left.end_seconds


def _time_window(value: object) -> OracleTimeWindow | None:
    if not isinstance(value, dict):
        return None
    try:
        return OracleTimeWindow.model_validate(value)
    except ValueError:
        return None


def _string_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _is_inspector_task(arguments: object) -> bool:
    return isinstance(arguments, dict) and arguments.get("subagent_type") == "evidence-inspector"


def _is_targeted_scout_task(arguments: object) -> bool:
    if not isinstance(arguments, dict) or arguments.get("subagent_type") != "evidence-scout":
        return False
    description = arguments.get("description")
    if not isinstance(description, str):
        return False
    try:
        payload = json.loads(description)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("phase") == "targeted_research"
