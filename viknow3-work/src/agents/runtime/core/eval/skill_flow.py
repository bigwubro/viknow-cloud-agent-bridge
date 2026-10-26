"""Offline scorers for full skill-flow eval traces."""

from __future__ import annotations

import ast
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.runtime.core.eval.quality_score import (
    OracleRequirementAssessment,
    QualityScorecard,
    score_efficiency_convergence,
    score_process_compliance,
    score_quality_scorecard,
    score_release_safety,
)

_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")
_BARE_SOURCE_ID_RE = re.compile(r"\b(?:chunk-[A-Za-z0-9_-]+|[A-Za-z0-9_-]+_seg_\d{4,})\b")
_OPTIONAL_WORKFLOW_TOOLS = {"query_refine", "read_file", "grep", "list_video_segments"}


class SkillEvalCase(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str
    question: str
    reference_answer: str = ""
    expected_tool_path: list[str] = Field(default_factory=list)
    expected_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    expected_modalities: list[str] = Field(default_factory=list)
    gold_citations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerDisposition(BaseModel):
    """Agent-owned declaration about whether its final message answers the question."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["answered", "unanswerable"]
    reason: str = ""


class RefusalJudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str


class RefusalJudgeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_refusal: bool
    reason: str


class RefusalJudge(Protocol):
    def judge_refusal(self, request: RefusalJudgeRequest) -> RefusalJudgeDecision: ...


class GoldToolSimulator:
    """Deterministic gold-result simulator for skill-flow eval tool calls."""

    def __init__(self, case: SkillEvalCase) -> None:
        gold_trace = case.metadata.get("gold_trace")
        self._steps = gold_trace if isinstance(gold_trace, list) else []
        self._cursor = 0
        self.failure: dict[str, Any] = {}

    @property
    def completed(self) -> bool:
        return self._cursor >= len(self._steps) and not self.failed

    @property
    def failed(self) -> bool:
        return bool(self.failure)

    def should_passthrough(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        if self.failed:
            return False
        self._skip_optional_steps_until(tool_name)
        expected = self._current_step()
        expected_tool = _tool_call_name(expected) if expected is not None else ""
        if tool_name != expected_tool:
            return True
        if expected is None:
            return True
        return not _match_tool_arguments(
            expected_tool,
            _tool_call_arguments(expected),
            arguments,
        )["matches"]

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self.failed:
            return None
        self._skip_optional_steps_until(tool_name)
        if self._cursor >= len(self._steps):
            self.failure = {
                "bucket": "unexpected_tool",
                "tool": tool_name,
                "arguments": arguments,
            }
            return None
        expected = self._steps[self._cursor]
        if not isinstance(expected, dict):
            self.failure = {"bucket": "invalid_gold_trace", "index": self._cursor}
            return None
        expected_tool = _tool_call_name(expected)
        if tool_name != expected_tool:
            self.failure = {
                "bucket": "wrong_tool",
                "expected_tool": expected_tool,
                "actual_tool": tool_name,
            }
            return None
        match = _match_tool_arguments(
            expected_tool,
            _tool_call_arguments(expected),
            arguments,
        )
        if not match["matches"]:
            buckets = _argument_failure_buckets(match["mismatches"])
            self.failure = {
                "bucket": buckets[0] if buckets else "wrong_tool_arguments",
                "buckets": buckets,
                "tool": tool_name,
                "mismatches": match["mismatches"],
            }
            return None
        self._cursor += 1
        return expected.get("ideal_result")

    def _current_step(self) -> dict[str, Any] | None:
        if self._cursor >= len(self._steps):
            return None
        expected = self._steps[self._cursor]
        return expected if isinstance(expected, dict) else None

    def _skip_optional_steps_until(self, tool_name: str) -> None:
        while self._cursor < len(self._steps):
            expected = self._current_step()
            if expected is None:
                return
            expected_tool = _tool_call_name(expected)
            if expected_tool == tool_name or not _is_optional_workflow_tool(expected_tool):
                return
            self._cursor += 1


@dataclass
class SkillEvalResult:
    case_id: str
    status: str
    question: str
    reference_answer: str
    final_answer: str | None
    expected_tool_path: list[str]
    actual_tool_path: list[str]
    workflow_result: dict[str, Any]
    tool_call_results: dict[str, Any]
    argument_result: dict[str, Any]
    target_result: dict[str, Any]
    evidence_results: dict[str, Any]
    citation_results: dict[str, Any]
    stop_gate_result: dict[str, Any]
    inspect_funnel_result: dict[str, Any]
    reference_routing_result: dict[str, Any]
    harness_result: dict[str, Any]
    answer_result: dict[str, Any]
    efficiency_result: dict[str, Any]
    query_decomposition_result: dict[str, Any]
    stability_result: dict[str, Any]
    oracle_result: dict[str, Any]
    quality_scorecard: dict[str, Any] | None
    verdict_result: dict[str, Any]
    failure_buckets: list[str] = field(default_factory=list)
    trace_id: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "question": self.question,
            "reference_answer": self.reference_answer,
            "final_answer": self.final_answer,
            "expected_tool_path": self.expected_tool_path,
            "actual_tool_path": self.actual_tool_path,
            "workflow_result": self.workflow_result,
            "tool_call_results": self.tool_call_results,
            "argument_result": self.argument_result,
            "target_result": self.target_result,
            "evidence_results": self.evidence_results,
            "citation_results": self.citation_results,
            "stop_gate_result": self.stop_gate_result,
            "inspect_funnel_result": self.inspect_funnel_result,
            "reference_routing_result": self.reference_routing_result,
            "harness_result": self.harness_result,
            "answer_result": self.answer_result,
            "efficiency_result": self.efficiency_result,
            "query_decomposition_result": self.query_decomposition_result,
            "stability_result": self.stability_result,
            "oracle_result": self.oracle_result,
            "quality_scorecard": self.quality_scorecard,
            "verdict_result": self.verdict_result,
            "failure_buckets": self.failure_buckets,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
        }


def score_case(
    case: SkillEvalCase,
    trace: Any,
    *,
    oracle_package: Any | None = None,
    oracle_assessments: list[OracleRequirementAssessment] | None = None,
    quality_scorer_version: str = "skill-flow-quality-v1",
    quality_judge_model: str | None = None,
    refusal_judge: RefusalJudge | None = None,
) -> SkillEvalResult:
    payload = _model_or_mapping(trace)
    status = str(payload.get("status") or "unknown")
    final_answer = _optional_str(payload.get("answer"))
    metadata = _model_or_mapping(payload.get("metadata"))
    disposition_payload = payload.get("answer_disposition") or metadata.get(
        "viknow_answer_disposition"
    )
    try:
        answer_disposition = (
            AnswerDisposition.model_validate(disposition_payload)
            if disposition_payload is not None
            else None
        )
    except ValidationError:
        answer_disposition = None
    performance = _model_or_mapping(payload.get("performance"))
    actual_tool_records = _tool_call_records(metadata)
    actual_tool_path = [record["name"] for record in actual_tool_records] or _flatten_tool_calls(
        metadata.get("tool_calls")
    )

    workflow_result = score_workflow(case.expected_tool_path, actual_tool_path)
    tool_call_results = score_tool_calls(case.expected_modalities, actual_tool_path)
    argument_result = score_arguments(case.expected_tool_calls, actual_tool_records)
    target_result = score_targets(
        case.expected_tool_calls or _expected_tool_calls_from_gold(case.gold_citations),
        actual_tool_records,
    )
    evidence_results = score_evidence(
        case.gold_citations,
        payload,
        actual_tool_path,
        actual_tool_records,
    )
    citation_results = score_citations(case, payload, final_answer, evidence_results)
    stop_gate_result = score_stop_gate(metadata, citation_results)
    inspect_funnel_result = score_inspect_funnel(
        case,
        payload,
        final_answer,
        actual_tool_records,
        citation_results,
        status=status,
    )
    reference_routing_result = score_reference_routing(actual_tool_records)
    harness_result = score_harness_facts(metadata)
    answer_result = score_answer(
        final_answer,
        status,
        reference_answer=case.reference_answer,
        answer_format=str(case.metadata.get("answer_format") or ""),
        question=case.question,
        answer_disposition=answer_disposition,
        refusal_judge=refusal_judge,
    )
    efficiency_result = score_efficiency(performance, actual_tool_path, stop_gate_result)
    query_decomposition_result = score_query_decomposition(
        question=case.question,
        gold_citations=case.gold_citations,
        trace=payload,
        actual_tool_records=actual_tool_records,
    )
    stability_result: dict[str, Any] = {}
    oracle_result = score_oracle_package(
        case=case,
        oracle_package=oracle_package,
        inspect_funnel_result=inspect_funnel_result,
    )
    quality_scorecard = _score_quality_scorecard(
        status=status,
        oracle_package=oracle_package,
        oracle_assessments=oracle_assessments,
        harness_result=harness_result,
        tool_call_records=actual_tool_records,
        stop_gate_result=stop_gate_result,
        citation_results=citation_results,
        scorer_version=quality_scorer_version,
        judge_model=quality_judge_model,
    )
    verdict_result = score_verdict(
        status=status,
        answer_result=answer_result,
        citation_results=citation_results,
        inspect_funnel_result=inspect_funnel_result,
        oracle_result=oracle_result,
    )

    failure_buckets = _failure_buckets(
        status=status,
        workflow_result=workflow_result,
        tool_call_results=tool_call_results,
        argument_result=argument_result,
        target_result=target_result,
        evidence_results=evidence_results,
        inspect_funnel_result=inspect_funnel_result,
        citation_results=citation_results,
        answer_result=answer_result,
    )

    return SkillEvalResult(
        case_id=case.case_id,
        status=status,
        question=case.question,
        reference_answer=case.reference_answer,
        final_answer=final_answer,
        expected_tool_path=list(case.expected_tool_path),
        actual_tool_path=actual_tool_path,
        workflow_result=workflow_result,
        tool_call_results=tool_call_results,
        argument_result=argument_result,
        target_result=target_result,
        evidence_results=evidence_results,
        citation_results=citation_results,
        stop_gate_result=stop_gate_result,
        inspect_funnel_result=inspect_funnel_result,
        reference_routing_result=reference_routing_result,
        harness_result=harness_result,
        answer_result=answer_result,
        efficiency_result=efficiency_result,
        query_decomposition_result=query_decomposition_result,
        stability_result=stability_result,
        oracle_result=oracle_result,
        quality_scorecard=(quality_scorecard.model_dump() if quality_scorecard else None),
        verdict_result=verdict_result,
        failure_buckets=failure_buckets,
        trace_id=_optional_str(payload.get("langfuse_trace_id")),
        run_id=_optional_str(payload.get("run_id")),
    )


def score_workflow(expected_tool_path: list[str], actual_tool_path: list[str]) -> dict[str, Any]:
    required_expected_path = _required_tool_path(expected_tool_path)
    required_actual_path = _required_tool_path(actual_tool_path)
    required_count = len(required_expected_path)
    expected_counter = Counter(required_expected_path)
    actual_counter = Counter(required_actual_path)
    called_required_count = sum(
        min(expected_count, actual_counter[tool])
        for tool, expected_count in expected_counter.items()
    )
    extra_tool_count = sum(
        max(0, actual_count - expected_counter[tool])
        for tool, actual_count in actual_counter.items()
    )
    ordered_match_count = _subsequence_match_count(required_expected_path, required_actual_path)
    pre_expected, post_expected = _split_search_phases(required_expected_path)
    pre_actual, post_actual = _split_search_phases(required_actual_path)
    return {
        "required_tool_count": required_count,
        "called_required_tool_count": called_required_count,
        "required_tool_recall": _ratio(called_required_count, required_count),
        "tool_order_score": _ratio(ordered_match_count, required_count),
        "extra_tool_count": extra_tool_count,
        "extra_tool_rate": _ratio(extra_tool_count, len(required_actual_path)),
        "missing_tools": _missing_tools(expected_counter, actual_counter),
        "optional_expected_tools": [
            tool for tool in expected_tool_path if _is_optional_workflow_tool(tool)
        ],
        "pre_search": _phase_workflow_result(pre_expected, pre_actual),
        "post_search": _phase_workflow_result(post_expected, post_actual),
    }


def score_tool_calls(expected_modalities: list[str], actual_tool_path: list[str]) -> dict[str, Any]:
    required = {_normalize_modality(item) for item in expected_modalities if item}
    actual = _modalities_from_tools(actual_tool_path)
    missing = sorted(required - actual)
    unnecessary = sorted(actual - required) if required else []
    return {
        "expected_modalities": sorted(required),
        "actual_modalities": sorted(actual),
        "missing_required_modalities": missing,
        "unnecessary_modalities": unnecessary,
        "modality_route_accuracy": 1.0 if not missing else 0.0,
    }


def score_reference_routing(actual_tool_records: list[dict[str, Any]]) -> dict[str, Any]:
    reference_reads: list[tuple[int, str]] = []
    for index, record in enumerate(actual_tool_records):
        if record.get("name") != "read_file":
            continue
        path = str((record.get("arguments") or {}).get("file_path") or "")
        marker = "/references/"
        if marker not in path:
            continue
        reference_reads.append((index, path.rsplit(marker, 1)[-1]))

    requirements: list[dict[str, Any]] = []
    required_by_tool = {"list_video_segments": "video-candidate-audit.md"}
    for tool_index, record in enumerate(actual_tool_records):
        dependent_tool = str(record.get("name") or "")
        required_reference = required_by_tool.get(dependent_tool)
        if required_reference is None:
            continue
        matching_indexes = [
            read_index
            for read_index, reference_file in reference_reads
            if reference_file == required_reference
        ]
        requirements.append(
            {
                "dependent_tool": dependent_tool,
                "dependent_tool_index": tool_index,
                "required_reference": required_reference,
                "reference_read": bool(matching_indexes),
                "reference_read_before_tool": any(
                    read_index < tool_index for read_index in matching_indexes
                ),
            }
        )

    required_reference_triggered = bool(requirements)
    return {
        "reference_read_calls": len(reference_reads),
        "reference_files": sorted({path for _, path in reference_reads}),
        "required_reference_triggered": required_reference_triggered,
        "required_reference_read": (
            all(item["reference_read"] for item in requirements)
            if required_reference_triggered
            else None
        ),
        "reference_read_before_dependent_tool": (
            all(item["reference_read_before_tool"] for item in requirements)
            if required_reference_triggered
            else None
        ),
        "requirements": requirements,
    }


def score_arguments(
    expected_tool_calls: list[dict[str, Any]],
    actual_tool_records: list[dict[str, Any]],
) -> dict[str, Any]:
    required_expected_calls = [
        call
        for call in expected_tool_calls
        if not _is_optional_workflow_tool(_tool_call_name(call))
    ]
    optional_expected_call_count = len(expected_tool_calls) - len(required_expected_calls)
    if not required_expected_calls:
        return {
            "required_call_count": 0,
            "matched_call_count": 0,
            "argument_match_rate": 1.0,
            "extra_attempt_count": 0,
            "optional_expected_call_count": optional_expected_call_count,
            "matched_calls": [],
            "unmatched_expected_calls": [],
        }

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    cursor = 0
    extra_attempt_count = 0
    used_indexes: set[int] = set()
    for expected_index, expected in enumerate(required_expected_calls):
        expected_name = _tool_call_name(expected)
        best: tuple[int, dict[str, Any]] | None = None
        skipped_same_tool = 0
        failed_candidates: list[tuple[int, dict[str, Any]]] = []
        for actual_index in range(cursor, len(actual_tool_records)):
            if actual_index in used_indexes:
                continue
            actual = actual_tool_records[actual_index]
            if _tool_call_name(actual) != expected_name:
                continue
            result = _match_tool_arguments(
                expected_name,
                _tool_call_arguments(expected),
                _tool_call_arguments(actual),
            )
            if result["matches"]:
                best = (actual_index, result)
                break
            skipped_same_tool += 1
            failed_candidates.append((actual_index, result))
        if best is None:
            unmatched_call = {
                "expected_index": expected_index,
                "tool": expected_name,
                "arguments": _tool_call_arguments(expected),
                "reason": "missing_tool_step",
            }
            if failed_candidates:
                _, best_failed = min(
                    failed_candidates,
                    key=lambda item: len(item[1].get("mismatches") or []),
                )
                unmatched_call["reason"] = "wrong_arguments"
                unmatched_call["best_mismatches"] = best_failed.get("mismatches") or []
            unmatched.append(unmatched_call)
            continue
        actual_index, result = best
        used_indexes.add(actual_index)
        cursor = actual_index + 1
        extra_attempt_count += skipped_same_tool
        matched.append(
            {
                "expected_index": expected_index,
                "actual_index": actual_index,
                "tool": expected_name,
                **result,
            }
        )

    return {
        "required_call_count": len(required_expected_calls),
        "matched_call_count": len(matched),
        "argument_match_rate": _ratio(len(matched), len(required_expected_calls)),
        "extra_attempt_count": extra_attempt_count,
        "optional_expected_call_count": optional_expected_call_count,
        "matched_calls": matched,
        "unmatched_expected_calls": unmatched,
    }


def score_targets(
    expected_tool_calls: list[dict[str, Any]],
    actual_tool_records: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_targets = [
        call
        for call in expected_tool_calls
        if _target_scored_tool(_tool_call_name(call))
        and not _is_optional_workflow_tool(_tool_call_name(call))
    ]
    if not expected_targets:
        return {
            "required_target_count": 0,
            "matched_target_count": 0,
            "target_match_rate": 1.0,
            "hard_failure_count": 0,
            "soft_issue_count": 0,
            "matched_targets": [],
            "unmatched_targets": [],
        }

    used_indexes: set[int] = set()
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for expected_index, expected in enumerate(expected_targets):
        expected_name = _tool_call_name(expected)
        best_match: tuple[int, dict[str, Any]] | None = None
        failed_candidates: list[dict[str, Any]] = []
        for actual_index, actual in enumerate(actual_tool_records):
            if actual_index in used_indexes or _tool_call_name(actual) != expected_name:
                continue
            candidate = _score_target_candidate(
                expected_name,
                _tool_call_arguments(expected),
                _tool_call_arguments(actual),
            )
            if candidate["hard_failures"]:
                failed_candidates.append(candidate)
                continue
            if best_match is None or candidate["score"] > best_match[1]["score"]:
                best_match = (actual_index, candidate)
        if best_match is None:
            unmatched.append(
                {
                    "expected_index": expected_index,
                    "tool": expected_name,
                    "arguments": _tool_call_arguments(expected),
                    "hard_failures": _fewest_hard_failures(failed_candidates),
                }
            )
            continue
        actual_index, candidate = best_match
        used_indexes.add(actual_index)
        matched.append(
            {
                "expected_index": expected_index,
                "actual_index": actual_index,
                "tool": expected_name,
                **candidate,
            }
        )

    score_sum = sum(float(item.get("score") or 0.0) for item in matched)
    return {
        "required_target_count": len(expected_targets),
        "matched_target_count": len(matched),
        "target_match_rate": _ratio(score_sum, len(expected_targets)),
        "hard_failure_count": len(unmatched),
        "soft_issue_count": sum(len(item.get("soft_issues") or []) for item in matched),
        "matched_targets": matched,
        "unmatched_targets": unmatched,
    }


def score_evidence(
    gold_citations: list[dict[str, Any]],
    trace: dict[str, Any],
    actual_tool_path: list[str],
    actual_tool_records: list[dict[str, Any]],
) -> dict[str, Any]:
    gold_assets = {
        str(gold.get("asset_id") or "")
        for gold in gold_citations
        if str(gold.get("asset_id") or "")
    }
    touched_assets = _touched_assets(trace)
    hit_assets = gold_assets & touched_assets
    inspect_required = bool(gold_citations)
    inspect_called = any(tool.endswith("_inspect") for tool in actual_tool_path)
    candidate_payloads = _candidate_tool_payloads(trace)
    inspect_observations = _inspect_observations(trace, actual_tool_records)
    funnel = [
        _gold_funnel_group(requirement_id, alternatives, candidate_payloads, inspect_observations)
        for requirement_id, alternatives in _gold_requirement_groups(gold_citations)
    ]
    exposed_count = sum(1 for item in funnel if item["candidate_exposed"])
    inspected_count = sum(1 for item in funnel if item["inspected"])
    inspect_hit_count = sum(1 for item in funnel if item["inspect_locator_hit"])
    return {
        "gold_citation_count": len(funnel),
        "gold_asset_count": len(gold_assets),
        "touched_gold_asset_count": len(hit_assets),
        "gold_asset_recall": _ratio(len(hit_assets), len(gold_assets)),
        "candidate_exposed_count": exposed_count,
        "candidate_exposure_recall": _ratio(exposed_count, len(funnel)),
        "inspected_gold_count": inspected_count,
        "inspect_required": inspect_required,
        "inspect_called": inspect_called,
        "inspect_empty_result_rate": None,
        "inspect_gold_recall": _ratio(inspect_hit_count, len(funnel)),
        "funnel": funnel,
    }


def _expected_tool_calls_from_gold(gold_citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for _, alternatives in _gold_requirement_groups(gold_citations):
        gold = alternatives[0]
        modality = _gold_modality(gold)
        if modality not in {"text", "video", "image"}:
            continue
        arguments: dict[str, Any] = {}
        if gold.get("workspace"):
            arguments["library_id"] = gold.get("workspace")
        if gold.get("asset_id"):
            arguments["asset_id"] = gold.get("asset_id")
        if modality == "video":
            arguments["time_range"] = gold.get("time_range")
        if modality == "text":
            arguments["evidence_id"] = gold.get("chunk_id")
        calls.append({"tool": f"{modality}_inspect", "arguments": arguments})
    return calls


def _gold_requirement_groups(
    gold_citations: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, gold in enumerate(gold_citations, start=1):
        requirement_id = str(gold.get("requirement_id") or gold.get("gold_id") or index)
        grouped.setdefault(requirement_id, []).append(gold)
    return list(grouped.items())


def _gold_funnel_group(
    requirement_id: str,
    alternatives: list[dict[str, Any]],
    candidate_payloads: list[dict[str, Any]],
    inspect_observations: list[dict[str, Any]],
) -> dict[str, Any]:
    items = [
        _gold_funnel_item(gold, candidate_payloads, inspect_observations) for gold in alternatives
    ]
    selected = max(
        items,
        key=lambda item: (
            bool(item["inspect_locator_hit"]),
            bool(item["inspected"]),
            bool(item["candidate_exposed"]),
        ),
    )
    return {
        **selected,
        "gold_id": requirement_id,
        "alternative_gold_ids": [str(gold.get("gold_id") or "") for gold in alternatives],
        "alternative_count": len(alternatives),
    }


def score_citations(
    case: SkillEvalCase,
    trace: dict[str, Any],
    final_answer: str | None,
    evidence_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_results = evidence_results or {}
    metadata = _model_or_mapping(trace.get("metadata"))
    citations = metadata.get("citations")
    citation_map = citations if isinstance(citations, dict) else {}
    used_markers = _answer_markers(final_answer or "")
    citation_keys = {str(key) for key in citation_map}
    citation_markers = {
        str(value.get("marker") or "").strip()
        for value in citation_map.values()
        if isinstance(value, dict)
    }
    unresolved_markers = [
        marker
        for marker in used_markers
        if marker.strip("[]") not in citation_keys and marker not in citation_markers
    ]
    malformed = [
        key
        for key, citation in citation_map.items()
        if not _citation_has_valid_locator(citation if isinstance(citation, dict) else {})
    ]
    needs_citation = bool(case.gold_citations and (final_answer or "").strip())
    missing_required_markers = needs_citation and not used_markers
    bare_source_ids = _BARE_SOURCE_ID_RE.findall(final_answer or "")
    format_error = bool(
        unresolved_markers or malformed or missing_required_markers or bare_source_ids
    )
    funnel = evidence_results.get("funnel") or []
    inspected_gold_items = [
        item for item in funnel if isinstance(item, dict) and item.get("inspect_locator_hit")
    ]
    gold_used_count = sum(
        1
        for item in inspected_gold_items
        if set(item.get("inspect_markers") or []) & set(used_markers)
    )
    non_gold_used_count = sum(
        1
        for marker in used_markers
        if marker
        not in {
            gold_marker
            for item in inspected_gold_items
            for gold_marker in item.get("inspect_markers") or []
        }
    )
    return {
        "citation_count": len(citation_map),
        "used_marker_count": len(used_markers),
        "unresolved_markers": unresolved_markers,
        "malformed_locator_keys": malformed,
        "missing_required_markers": missing_required_markers,
        "bare_source_ids": bare_source_ids,
        "citation_format_error": format_error,
        "gold_citation_count": len(funnel),
        "agent_gold_usage_recall": _ratio(gold_used_count, len(funnel)),
        "agent_non_gold_usage_rate": _ratio(non_gold_used_count, len(used_markers)),
        "inspect_to_answer_drop_rate": _ratio(
            len(inspected_gold_items) - gold_used_count,
            len(inspected_gold_items),
        ),
    }


def score_inspect_funnel(
    case: SkillEvalCase,
    trace: dict[str, Any],
    final_answer: str | None,
    actual_tool_records: list[dict[str, Any]],
    citation_results: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Score independently diagnosable and cumulative Inspect evidence stages."""
    metadata = _model_or_mapping(trace.get("metadata"))
    uses_stable_projection = bool(metadata.get("inspect_result_summaries"))
    candidate_events = _candidate_tool_events(trace)
    inspect_observations = _ordered_inspect_observations(trace, actual_tool_records)
    final_citations = _used_final_citations(trace, final_answer or "")
    items = [
        _inspect_funnel_group(
            requirement_id,
            alternatives,
            candidate_events,
            inspect_observations,
            final_citations,
        )
        for requirement_id, alternatives in _gold_requirement_groups(case.gold_citations)
    ]
    case_hard_pass = bool(items) and all(item["final_citation_matched"] for item in items)
    case_hard_pass = (
        case_hard_pass
        and status == "completed"
        and not citation_results.get("citation_format_error")
    )
    publishable = status == "completed" and not citation_results.get("citation_format_error")
    video_items = [item for item in items if item.get("modality") == "video"]
    case_used_claim_locator_pass = (
        bool(video_items)
        and publishable
        and all(item["gold_with_used_claim_locator_hit"] for item in video_items)
    )
    case_used_claim_union_pass = (
        bool(video_items)
        and publishable
        and all(item["used_claim_union_covered"] for item in video_items)
    )
    return {
        "gold_count": len(items),
        "items": items,
        "uses_stable_projection": uses_stable_projection,
        "case_hard_pass": case_hard_pass,
        "case_used_claim_locator_pass": case_used_claim_locator_pass,
        "case_used_claim_union_pass": case_used_claim_union_pass,
    }


def score_answer(
    final_answer: str | None,
    status: str,
    *,
    reference_answer: str = "",
    answer_format: str = "",
    question: str = "",
    answer_disposition: AnswerDisposition | None = None,
    refusal_judge: RefusalJudge | None = None,
) -> dict[str, Any]:
    answer_empty = not bool((final_answer or "").strip())
    answer_point_recall = _reference_overlap_recall(reference_answer, final_answer or "")
    return {
        "answer_empty": answer_empty,
        "task_success": status == "completed" and not answer_empty,
        "reference_answer_present": bool(reference_answer.strip()),
        "answer_point_recall": answer_point_recall,
        "answer_conflict_rate": None,
        "reference_assessment": _structured_reference_assessment(
            reference_answer=reference_answer,
            final_answer=final_answer or "",
            answer_format=answer_format,
            question=question,
            answer_disposition=answer_disposition,
            refusal_judge=refusal_judge,
        ),
    }


def score_oracle_package(
    *,
    case: SkillEvalCase,
    oracle_package: Any | None,
    inspect_funnel_result: dict[str, Any],
) -> dict[str, Any]:
    """Project a scope-bound oracle package into verdict-ready trace facts.

    The package states what the offline evaluator can require. The funnel still
    determines whether this particular trace inspected and cited that evidence.
    """
    if oracle_package is None:
        return {"enabled": False}
    if str(getattr(oracle_package, "case_id", "")) != case.case_id:
        raise ValueError("oracle package case_id does not match scored case")

    funnel_items = [
        item for item in inspect_funnel_result.get("items") or [] if isinstance(item, dict)
    ]
    supported: list[str] = []
    contradicted: list[str] = []
    unresolved: list[str] = []
    satisfied: list[str] = []
    unsatisfied: list[str] = []
    for requirement in getattr(oracle_package, "requirements", []):
        requirement_id = str(getattr(requirement, "requirement_id", ""))
        disposition = str(getattr(requirement, "disposition", "unknown"))
        if disposition == "supports":
            supported.append(requirement_id)
            gold_ids = list(getattr(requirement, "gold_ids", []))
            if any(
                item.get("inspect_chain_completed")
                and str(gold_id) in (item.get("alternative_gold_ids") or [])
                for item in funnel_items
                for gold_id in gold_ids
            ):
                satisfied.append(requirement_id)
            else:
                unsatisfied.append(requirement_id)
        elif disposition == "contradicts":
            contradicted.append(requirement_id)
        else:
            unresolved.append(requirement_id)

    return {
        "enabled": True,
        "storage_key": str(getattr(oracle_package, "storage_key", "")),
        "requirement_count": len(getattr(oracle_package, "requirements", [])),
        "supported_requirement_ids": supported,
        "contradicted_requirement_ids": contradicted,
        "unresolved_requirement_ids": unresolved,
        "satisfied_requirement_ids": satisfied,
        "unsatisfied_requirement_ids": unsatisfied,
    }


def score_verdict(
    *,
    status: str,
    answer_result: dict[str, Any],
    citation_results: dict[str, Any],
    inspect_funnel_result: dict[str, Any],
    oracle_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the top-level case conclusion without hiding diagnostic metrics."""

    if status != "completed":
        return {"verdict": "insufficient_evidence", "reasons": ["run_not_completed"]}
    if answer_result.get("answer_empty"):
        return {"verdict": "fail", "reasons": ["answer_empty"]}
    if citation_results.get("citation_format_error"):
        return {"verdict": "fail", "reasons": ["citation_format_error"]}
    reference_assessment = answer_result.get("reference_assessment")
    if isinstance(reference_assessment, dict) and reference_assessment.get("matched") is False:
        return {"verdict": "fail", "reasons": ["reference_answer_mismatch"]}
    if oracle_result and oracle_result.get("enabled"):
        if oracle_result.get("unresolved_requirement_ids"):
            return {
                "verdict": "insufficient_evidence",
                "reasons": ["oracle_requirement_unresolved"],
            }
        if oracle_result.get("contradicted_requirement_ids"):
            return {
                "verdict": "fail",
                "reasons": ["oracle_evidence_contradicted"],
            }
        if oracle_result.get("unsatisfied_requirement_ids"):
            return {
                "verdict": "fail",
                "reasons": ["answer_unsupported_by_oracle_evidence"],
            }
    if inspect_funnel_result.get("gold_count") and not inspect_funnel_result.get("case_hard_pass"):
        return {
            "verdict": "fail",
            "reasons": ["answer_unsupported_by_gold_evidence"],
        }
    return {"verdict": "pass", "reasons": []}


def score_harness_facts(metadata: dict[str, Any]) -> dict[str, Any]:
    """Summarize deterministic runtime facts without turning them into quality verdicts."""

    raw_signals = metadata.get("run_signals")
    if not isinstance(raw_signals, list):
        return {"available": False}
    signals = [_model_or_mapping(item) for item in raw_signals]
    kinds = sorted({str(signal.get("kind") or "") for signal in signals if signal.get("kind")})
    warning_kinds = sorted(
        {
            str(signal.get("kind") or "")
            for signal in signals
            if signal.get("kind") and signal.get("severity") == "warning"
        }
    )
    return {
        "available": True,
        "signal_count": len(signals),
        "warning_signal_count": sum(1 for signal in signals if signal.get("severity") == "warning"),
        "signal_kinds": kinds,
        "warning_signal_kinds": warning_kinds,
        "signals": signals,
    }


def score_efficiency(
    performance: dict[str, Any],
    actual_tool_path: list[str],
    stop_gate_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counter = Counter(actual_tool_path)
    spans = _model_spans(performance)
    final_span = spans[-1] if spans else {}
    span_latency = [
        float(item["latency_seconds"])
        for item in spans
        if isinstance(item.get("latency_seconds"), (int, float))
    ]
    total_span_latency = sum(span_latency)
    stopped_blocked = bool((stop_gate_result or {}).get("blocked"))
    return {
        "latency_seconds": _optional_float(
            performance.get("total_cost") or performance.get("latency_seconds")
        ),
        "ttft_seconds": _optional_float(performance.get("ttft")),
        "token_input": _optional_int(performance.get("token_in")),
        "token_output": _optional_int(performance.get("token_out")),
        "tool_call_count": _optional_int(performance.get("tool_call_count"))
        or len(actual_tool_path),
        "inspect_call_count": sum(
            count for tool, count in counter.items() if tool.endswith("_inspect")
        ),
        "knowledge_search_count": counter["knowledge_search"],
        "repeated_search_count": max(0, counter["knowledge_search"] - 1),
        "model_call_count": _optional_int(performance.get("thinking_step")),
        "model_span_count": len(spans),
        "model_spans_total_latency": round(total_span_latency, 6) if span_latency else None,
        "final_answer_latency": _optional_float(final_span.get("latency_seconds")),
        "final_answer_token_in": _optional_int(final_span.get("token_in")),
        "final_answer_token_out": _optional_int(final_span.get("token_out")),
        "final_answer_tool_calls": final_span.get("tool_calls") or [],
        "stop_gate_blocked": stopped_blocked,
        "post_gate_model_calls": _count_post_gate_model_calls(spans, stopped_blocked),
        "tool_counts": dict(counter),
    }


def score_query_decomposition(
    *,
    question: str,
    gold_citations: list[dict[str, Any]],
    trace: dict[str, Any],
    actual_tool_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project query planning and per-library search execution from one trace."""

    metadata = _model_or_mapping(trace.get("metadata"))
    projected_plans = [
        _model_or_mapping(payload)
        for payload in metadata.get("query_refine_plans") or []
        if isinstance(payload, dict)
    ]
    plans = projected_plans or [
        payload
        for payload in _trace_tool_payloads(trace)
        if str(payload.get("tool") or "") == "query_refine" and not payload.get("error")
    ]
    latest_plan = plans[-1] if plans else {}
    planned_queries = _unique_normalized_queries(
        item.get("query")
        for item in latest_plan.get("retrieval_queries") or []
        if isinstance(item, dict)
    )
    user_query_key = _normalized_search_query(question)
    plan_original_key = _normalized_search_query(latest_plan.get("original_query") or question)
    seen_search_keys: set[tuple[str, str]] = set()
    executed_query_keys: set[str] = set()
    searches: list[dict[str, Any]] = []
    projected_searches = [
        _model_or_mapping(payload)
        for payload in metadata.get("search_result_summaries") or []
        if isinstance(payload, dict)
    ]
    search_events = [
        {
            "call_id": item.get("tool_call_id"),
            "arguments": {
                "library_id": item.get("library_id"),
                "query": item.get("query"),
            },
            "result": {"evidence": item.get("candidates") or []},
        }
        for item in projected_searches
    ] or _ordered_tool_call_results(trace, "knowledge_search")
    search_records = (
        search_events
        if search_events
        else [
            {
                "call_id": record.get("id"),
                "arguments": _tool_call_arguments(record),
                "result": {},
            }
            for record in actual_tool_records
            if _tool_call_name(record) == "knowledge_search"
        ]
    )

    for record in search_records:
        arguments = _model_or_mapping(record.get("arguments"))
        library_id = str(arguments.get("library_id") or "").strip()
        query = str(arguments.get("query") or "").strip()
        normalized_query = _normalized_search_query(query)
        search_key = (library_id, normalized_query)
        duplicate = bool(normalized_query and search_key in seen_search_keys)
        if normalized_query:
            seen_search_keys.add(search_key)
            executed_query_keys.add(normalized_query)
        result = _model_or_mapping(record.get("result"))
        searches.append(
            {
                "call_id": record.get("call_id"),
                "library_id": library_id,
                "query": query,
                "normalized_query": normalized_query,
                "is_original_query": bool(
                    normalized_query and normalized_query == plan_original_key
                ),
                "duplicate": duplicate,
                "candidate_count": len(_candidate_evidence_items(result)),
                "gold_requirement_ids": _gold_requirements_in_candidate_payload(
                    result, gold_citations
                ),
            }
        )

    refine_records = [
        record for record in actual_tool_records if _tool_call_name(record) == "query_refine"
    ]
    refine_inputs_match = [
        _normalized_search_query(str(_tool_call_arguments(record).get("query") or ""))
        == user_query_key
        for record in refine_records
    ]
    executed_planned_count = sum(
        1 for query_key in planned_queries if query_key in executed_query_keys
    )
    duplicate_count = sum(1 for search in searches if search["duplicate"])
    return {
        "available": bool(refine_records or searches),
        "query_refine_call_count": len(refine_records),
        "query_refine_input_matches_user_query": bool(refine_inputs_match)
        and all(refine_inputs_match),
        "planned_query_count": len(planned_queries),
        "executed_planned_query_count": executed_planned_count,
        "planned_query_execution_rate": (
            _ratio(executed_planned_count, len(planned_queries)) if planned_queries else None
        ),
        "original_query_executed": bool(
            plan_original_key and plan_original_key in executed_query_keys
        ),
        "user_query_executed": bool(user_query_key and user_query_key in executed_query_keys),
        "search_call_count": len(searches),
        "unique_search_count": len(searches) - duplicate_count,
        "duplicate_search_count": duplicate_count,
        "searches": searches,
    }


def _normalized_search_query(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return text.strip(" ，,。！？!?；;：:")


def _unique_normalized_queries(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_search_query(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _ordered_tool_call_results(trace: dict[str, Any], tool_name: str) -> list[dict[str, Any]]:
    from agents.runtime.core.tools.base import unwrap_tool_data

    pending: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for message in trace.get("context") or []:
        payload = _model_or_mapping(message)
        for tool_call in payload.get("tool_calls") or []:
            call_payload = _model_or_mapping(tool_call)
            function = _model_or_mapping(call_payload.get("function"))
            if str(function.get("name") or "") != tool_name:
                continue
            call_id = str(call_payload.get("id") or "")
            if call_id:
                pending[call_id] = _json_mapping(function.get("arguments"))
        call_id = str(payload.get("tool_call_id") or "")
        if call_id in pending:
            results.append(
                {
                    "call_id": call_id,
                    "arguments": pending.pop(call_id),
                    "result": unwrap_tool_data(_json_object_prefix(payload.get("content"))),
                }
            )
    return results


def _gold_requirements_in_candidate_payload(
    payload: dict[str, Any], gold_citations: list[dict[str, Any]]
) -> list[str]:
    matched: list[str] = []
    for requirement_id, alternatives in _gold_requirement_groups(gold_citations):
        if any(_candidate_payload_matches_gold(payload, gold) for gold in alternatives):
            matched.append(requirement_id)
    return matched


def _model_spans(performance: dict[str, Any]) -> list[dict[str, Any]]:
    raw = performance.get("model_spans")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _count_post_gate_model_calls(
    spans: list[dict[str, Any]],
    stopped_blocked: bool,
) -> int:
    """Model rounds executed after an immediate stop-gate rejection.

    The gate rejects the pre-gate final answer, so the rounds after the last
    tool-calling span are rewrites or additional tool exploration. When the
    run was never blocked, post-gate rounds are zero by definition.
    """
    if not stopped_blocked or not spans:
        return 0
    # Find the last span that planned a tool call; everything after it is
    # post-gate continuation (rewrite with citations or extra tool calls).
    last_tool_plan_index: int | None = None
    for index, span in enumerate(spans):
        if span.get("tool_calls"):
            last_tool_plan_index = index
    if last_tool_plan_index is None:
        return 0
    return max(0, len(spans) - last_tool_plan_index - 1)


def summarize_results(results: list[SkillEvalResult]) -> dict[str, Any]:
    case_count = len(results)
    completed = [result for result in results if result.status == "completed"]
    failure_buckets = Counter(bucket for result in results for bucket in result.failure_buckets)
    verdicts = Counter(
        str(result.verdict_result.get("verdict") or "insufficient_evidence") for result in results
    )
    return {
        "case_count": case_count,
        "completed_case_count": len(completed),
        "run_failure_rate": _ratio(case_count - len(completed), case_count),
        "required_tool_recall_avg": _avg(
            result.workflow_result.get("required_tool_recall") for result in results
        ),
        "tool_order_score_avg": _avg(
            result.workflow_result.get("tool_order_score") for result in results
        ),
        "modality_route_accuracy_avg": _avg(
            result.tool_call_results.get("modality_route_accuracy") for result in results
        ),
        "target_match_rate_avg": _avg(
            result.target_result.get("target_match_rate") for result in results
        ),
        "candidate_exposure_recall_avg": _avg(
            result.evidence_results.get("candidate_exposure_recall") for result in results
        ),
        "inspect_gold_recall_avg": _avg(
            result.evidence_results.get("inspect_gold_recall") for result in results
        ),
        "argument_match_rate_avg": _avg(
            result.argument_result.get("argument_match_rate") for result in results
        ),
        "agent_gold_usage_recall_avg": _avg(
            result.citation_results.get("agent_gold_usage_recall") for result in results
        ),
        "inspect_to_answer_drop_rate_avg": _avg(
            result.citation_results.get("inspect_to_answer_drop_rate") for result in results
        ),
        "citation_format_error_rate": _ratio(
            sum(1 for result in results if result.citation_results.get("citation_format_error")),
            case_count,
        ),
        "verdicts": _verdict_counts(verdicts),
        "oracle": _summarize_oracle(results),
        "query_decomposition": _summarize_query_decomposition(results),
        "harness_facts": _summarize_harness_facts(results),
        "stop_gate": _summarize_stop_gate(results),
        "inspect_funnel": _summarize_inspect_funnel(results),
        "reference_routing": _summarize_reference_routing(results),
        "failure_buckets": dict(sorted(failure_buckets.items())),
        "cost": _summarize_cost(results),
        "tool_usage": _summarize_tool_usage(results),
    }


def _summarize_query_decomposition(results: list[SkillEvalResult]) -> dict[str, Any]:
    available = [
        result.query_decomposition_result
        for result in results
        if result.query_decomposition_result.get("available")
    ]
    refined = [item for item in available if item.get("query_refine_call_count")]
    planned_count = sum(int(item.get("planned_query_count") or 0) for item in refined)
    executed_count = sum(int(item.get("executed_planned_query_count") or 0) for item in refined)
    search_count = sum(int(item.get("search_call_count") or 0) for item in available)
    duplicate_count = sum(int(item.get("duplicate_search_count") or 0) for item in available)
    original_count = sum(1 for item in refined if item.get("original_query_executed"))
    return {
        "available_case_count": len(available),
        "query_refine_case_count": len(refined),
        "query_refine_usage_rate": _ratio(len(refined), len(available)),
        "original_query_executed_case_count": original_count,
        "original_query_execution_rate": _ratio(original_count, len(refined)),
        "planned_query_count": planned_count,
        "executed_planned_query_count": executed_count,
        "planned_query_execution_rate": (
            _ratio(executed_count, planned_count) if planned_count else None
        ),
        "search_call_count": search_count,
        "duplicate_search_count": duplicate_count,
        "duplicate_search_rate": _ratio(duplicate_count, search_count),
    }


def _summarize_oracle(results: list[SkillEvalResult]) -> dict[str, int]:
    enabled = [result.oracle_result for result in results if result.oracle_result.get("enabled")]
    return {
        "enabled_case_count": len(enabled),
        "case_count": len(results),
        "requirement_count": sum(int(result.get("requirement_count") or 0) for result in enabled),
        "supported_requirement_count": sum(
            len(result.get("supported_requirement_ids") or []) for result in enabled
        ),
        "contradicted_requirement_count": sum(
            len(result.get("contradicted_requirement_ids") or []) for result in enabled
        ),
        "unresolved_requirement_count": sum(
            len(result.get("unresolved_requirement_ids") or []) for result in enabled
        ),
        "satisfied_requirement_count": sum(
            len(result.get("satisfied_requirement_ids") or []) for result in enabled
        ),
        "unsatisfied_requirement_count": sum(
            len(result.get("unsatisfied_requirement_ids") or []) for result in enabled
        ),
    }


def _verdict_counts(counts: Counter[str]) -> dict[str, int]:
    return {
        verdict: counts.get(verdict, 0)
        for verdict in ("pass", "fail", "conditional_pass", "insufficient_evidence")
    }


def _summarize_harness_facts(results: list[SkillEvalResult]) -> dict[str, Any]:
    available = [
        result.harness_result for result in results if result.harness_result.get("available")
    ]
    signal_kinds = Counter(
        kind for result in available for kind in result.get("signal_kinds") or []
    )
    return {
        "available_case_count": len(available),
        "case_count": len(results),
        "signal_count": sum(int(result.get("signal_count") or 0) for result in available),
        "warning_signal_count": sum(
            int(result.get("warning_signal_count") or 0) for result in available
        ),
        "signal_kinds": dict(sorted(signal_kinds.items())),
    }


def score_stop_gate(metadata: dict[str, Any], citation_results: dict[str, Any]) -> dict[str, Any]:
    snapshot = _model_or_mapping(metadata.get("stop_gate"))
    if not snapshot.get("evaluated"):
        return {"available": False}
    pre_marker_count = int(snapshot.get("pre_gate_marker_count") or 0)
    post_marker_count = int(citation_results.get("used_marker_count") or 0)
    post_markers_resolved = bool(post_marker_count) and not bool(
        citation_results.get("unresolved_markers")
    )
    blocked = bool(snapshot.get("blocked"))
    reason_code = str(snapshot.get("reason_code") or "")
    signals = [_model_or_mapping(item) for item in metadata.get("run_signals") or []]
    signal_kinds = {str(signal.get("kind") or "") for signal in signals}
    return {
        "available": True,
        "blocked": blocked,
        "reason_code": reason_code,
        "pre_gate_marker_count": pre_marker_count,
        "pre_gate_resolved_citation_count": int(
            snapshot.get("pre_gate_resolved_citation_count") or 0
        ),
        "post_gate_marker_count": post_marker_count,
        "post_gate_markers_resolved": post_markers_resolved,
        "citation_marker_recovered": (
            blocked
            and reason_code == "missing_citation"
            and pre_marker_count == 0
            and post_markers_resolved
        ),
        "citation_marker_regressed": pre_marker_count > 0 and not post_markers_resolved,
        "recovered": "stop_gate_recovered" in signal_kinds,
        "released": "stop_gate_released" in signal_kinds,
    }


def _score_quality_scorecard(
    *,
    status: str,
    oracle_package: Any | None,
    oracle_assessments: list[OracleRequirementAssessment] | None,
    harness_result: dict[str, Any],
    tool_call_records: list[dict[str, Any]],
    stop_gate_result: dict[str, Any],
    citation_results: dict[str, Any],
    scorer_version: str,
    judge_model: str | None,
) -> QualityScorecard | None:
    """Combine supplied semantic assessments with content-free runtime facts.

    Semantic assessments are deliberately supplied by the offline runner. A
    plain ``score_case`` call must remain deterministic and model-free.
    """
    if oracle_package is None or oracle_assessments is None:
        return None
    process_dimension = score_process_compliance(
        signals=list(harness_result.get("signals") or []),
        tool_call_records=tool_call_records,
    )
    efficiency_dimension = score_efficiency_convergence(
        signals=list(harness_result.get("signals") or []),
        tool_call_records=tool_call_records,
    )
    release_dimension = score_release_safety(
        status=status,
        stop_gate=stop_gate_result,
        citation_results=citation_results,
    )
    return score_quality_scorecard(
        requirements=oracle_assessments,
        process_rules=process_dimension.rules,
        efficiency_rules=efficiency_dimension.rules,
        release_rules=release_dimension.rules,
        scorer_version=scorer_version,
        judge_model=judge_model,
    )


def _summarize_stop_gate(results: list[SkillEvalResult]) -> dict[str, Any]:
    available = [
        result.stop_gate_result for result in results if result.stop_gate_result.get("available")
    ]
    if not available:
        return {"available_case_count": 0, "case_count": len(results)}
    return {
        "available_case_count": len(available),
        "case_count": len(results),
        "evaluated_rate": _ratio(len(available), len(results)),
        "blocked_case_count": sum(1 for item in available if item.get("blocked")),
        "blocked_rate": _ratio(sum(1 for item in available if item.get("blocked")), len(available)),
        "missing_citation_block_count": sum(
            1 for item in available if item.get("reason_code") == "missing_citation"
        ),
        "citation_marker_recovered_count": sum(
            1 for item in available if item.get("citation_marker_recovered")
        ),
        "citation_marker_recovery_rate": _ratio(
            sum(1 for item in available if item.get("citation_marker_recovered")),
            sum(1 for item in available if item.get("reason_code") == "missing_citation"),
        )
        if any(item.get("reason_code") == "missing_citation" for item in available)
        else None,
        "citation_marker_regressed_count": sum(
            1 for item in available if item.get("citation_marker_regressed")
        ),
    }


def _summarize_cost(results: list[SkillEvalResult]) -> dict[str, Any]:
    latency = _numeric_values(result.efficiency_result.get("latency_seconds") for result in results)
    ttft = _numeric_values(result.efficiency_result.get("ttft_seconds") for result in results)
    token_input = _numeric_values(result.efficiency_result.get("token_input") for result in results)
    token_output = _numeric_values(
        result.efficiency_result.get("token_output") for result in results
    )
    tool_calls = _numeric_values(
        result.efficiency_result.get("tool_call_count") for result in results
    )
    final_latency = _numeric_values(
        result.efficiency_result.get("final_answer_latency") for result in results
    )
    final_token_in = _numeric_values(
        result.efficiency_result.get("final_answer_token_in") for result in results
    )
    final_token_out = _numeric_values(
        result.efficiency_result.get("final_answer_token_out") for result in results
    )
    post_gate_calls = _numeric_values(
        result.efficiency_result.get("post_gate_model_calls") for result in results
    )
    return {
        "latency_seconds": _distribution(latency),
        "ttft_seconds": _distribution(ttft),
        "tool_call_count": _distribution(tool_calls),
        "token_input_total": int(sum(token_input)),
        "token_input_avg": _avg(token_input),
        "token_output_total": int(sum(token_output)),
        "token_output_avg": _avg(token_output),
        "token_total": int(sum(token_input) + sum(token_output)),
        "latency_seconds_avg": _avg(latency),
        "tool_call_count_avg": _avg(tool_calls),
        "final_answer_latency": _distribution(final_latency),
        "final_answer_latency_avg": _avg(final_latency),
        "final_answer_token_input_total": int(sum(final_token_in)),
        "final_answer_token_output_total": int(sum(final_token_out)),
        "final_answer_token_total": int(sum(final_token_in) + sum(final_token_out)),
        "post_gate_model_calls_total": int(sum(post_gate_calls)),
        "post_gate_model_calls_avg": _avg(post_gate_calls),
        "blocked_case_count": sum(
            1 for result in results if result.efficiency_result.get("stop_gate_blocked")
        ),
    }


def _summarize_tool_usage(results: list[SkillEvalResult]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for result in results:
        counts.update(result.efficiency_result.get("tool_counts") or {})
    case_count = len(results)
    total_calls = sum(counts.values())
    summary = {
        "all": {
            "total": total_calls,
            "avg_per_case": _ratio(total_calls, case_count),
        }
    }
    for tool_name in sorted(counts):
        summary[tool_name] = {
            "total": counts[tool_name],
            "avg_per_case": _ratio(counts[tool_name], case_count),
        }
    return summary


def _numeric_values(values: Any) -> list[float]:
    return [float(value) for value in values if isinstance(value, int | float)]


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "total": round(sum(values), 6),
        "avg": _avg(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _summarize_inspect_funnel(results: list[SkillEvalResult]) -> dict[str, Any]:
    items = [
        item
        for result in results
        for item in result.inspect_funnel_result.get("items") or []
        if isinstance(item, dict)
    ]
    gold_count = len(items)
    hit_counts = {
        stage: sum(1 for item in items if item.get(stage))
        for stage in (
            "candidate_exposed",
            "inspect_targeted",
            "inspect_after_exposure",
            "inspect_return_eligible",
            "inspect_return_aligned",
            "final_citation_matched",
            "inspect_chain_completed",
        )
    }
    eligible_counts = {
        "candidate_exposed": gold_count,
        "inspect_targeted": gold_count,
        "inspect_after_exposure": hit_counts["candidate_exposed"],
        "inspect_return_eligible": hit_counts["inspect_after_exposure"],
        "inspect_return_aligned": hit_counts["inspect_return_eligible"],
        "final_citation_matched": gold_count,
        "inspect_chain_completed": hit_counts["inspect_return_aligned"],
    }
    summary: dict[str, Any] = {"gold_count": gold_count}
    for stage, hit_count in hit_counts.items():
        eligible_count = eligible_counts[stage]
        summary[stage] = {
            "hit_count": hit_count,
            "eligible_count": eligible_count,
            "cumulative_rate": _ratio(hit_count, gold_count),
            "conditional_rate": (_ratio(hit_count, eligible_count) if eligible_count else None),
        }
    hard_pass_count = sum(
        1 for result in results if result.inspect_funnel_result.get("case_hard_pass")
    )
    summary["case_hard_pass_count"] = hard_pass_count
    summary["case_hard_pass_rate"] = _ratio(hard_pass_count, len(results))
    summary["case_used_claim_locator_pass_count"] = sum(
        1 for result in results if result.inspect_funnel_result.get("case_used_claim_locator_pass")
    )
    summary["case_used_claim_union_pass_count"] = sum(
        1 for result in results if result.inspect_funnel_result.get("case_used_claim_union_pass")
    )
    source_counts = {
        stage: sum(1 for item in items if item.get(stage))
        for stage in (
            "search_candidate_exposed",
            "list_candidate_exposed",
            "list_incremental_exposed",
            "search_candidate_inspected",
            "list_candidate_inspected",
            "list_incremental_candidate_inspected",
        )
    }
    summary["candidate_sources"] = {
        **source_counts,
        "search_candidate_inspect_rate": _ratio(
            source_counts["search_candidate_inspected"],
            source_counts["search_candidate_exposed"],
        )
        if source_counts["search_candidate_exposed"]
        else None,
        "list_candidate_inspect_rate": _ratio(
            source_counts["list_candidate_inspected"],
            source_counts["list_candidate_exposed"],
        )
        if source_counts["list_candidate_exposed"]
        else None,
        "list_incremental_candidate_inspect_rate": _ratio(
            source_counts["list_incremental_candidate_inspected"],
            source_counts["list_incremental_exposed"],
        )
        if source_counts["list_incremental_exposed"]
        else None,
    }
    summary["claim_coverage"] = _summarize_claim_coverage_items(items)
    return summary


def _summarize_claim_coverage_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    items = [item for item in items if item.get("modality") == "video"]
    gold_count = len(items)
    claim_locator_hit_count = sum(int(item.get("claim_locator_hit_count") or 0) for item in items)
    used_claim_count = sum(int(item.get("used_claim_count") or 0) for item in items)
    used_claim_locator_hit_count = sum(
        int(item.get("used_claim_locator_hit_count") or 0) for item in items
    )
    result = {
        "gold_count": gold_count,
        "claim_locator_hit_count": claim_locator_hit_count,
        "gold_with_claim_locator_hit_count": sum(
            1 for item in items if item.get("gold_with_claim_locator_hit")
        ),
        "claim_union_covered_count": sum(1 for item in items if item.get("claim_union_covered")),
        "single_claim_aligned_count": sum(1 for item in items if item.get("single_claim_aligned")),
        "claim_union_coverage_avg": _avg(item.get("claim_union_coverage_ratio") for item in items),
        "used_claim_count": used_claim_count,
        "used_claim_locator_hit_count": used_claim_locator_hit_count,
        "gold_with_used_claim_locator_hit_count": sum(
            1 for item in items if item.get("gold_with_used_claim_locator_hit")
        ),
        "used_claim_union_covered_count": sum(
            1 for item in items if item.get("used_claim_union_covered")
        ),
        "used_single_claim_aligned_count": sum(
            1 for item in items if item.get("used_single_claim_aligned")
        ),
        "used_claim_union_coverage_avg": _avg(
            item.get("used_claim_union_coverage_ratio") for item in items
        ),
    }
    result["gold_with_claim_locator_hit_rate"] = (
        _ratio(result["gold_with_claim_locator_hit_count"], gold_count) if gold_count else None
    )
    result["claim_union_covered_rate"] = (
        _ratio(result["claim_union_covered_count"], gold_count) if gold_count else None
    )
    result["gold_with_used_claim_locator_hit_rate"] = (
        _ratio(result["gold_with_used_claim_locator_hit_count"], gold_count) if gold_count else None
    )
    result["used_claim_union_covered_rate"] = (
        _ratio(result["used_claim_union_covered_count"], gold_count) if gold_count else None
    )
    return result


def _summarize_reference_routing(results: list[SkillEvalResult]) -> dict[str, Any]:
    triggered = [
        result
        for result in results
        if result.reference_routing_result.get("required_reference_triggered")
    ]
    read_cases = [
        result for result in results if result.reference_routing_result.get("reference_read_calls")
    ]
    required_read = [
        result
        for result in triggered
        if result.reference_routing_result.get("required_reference_read")
    ]
    ordered = [
        result
        for result in triggered
        if result.reference_routing_result.get("reference_read_before_dependent_tool")
    ]
    files: Counter[str] = Counter()
    for result in results:
        files.update(result.reference_routing_result.get("reference_files") or [])
    return {
        "reference_read_calls": sum(
            int(result.reference_routing_result.get("reference_read_calls") or 0)
            for result in results
        ),
        "reference_read_case_count": len(read_cases),
        "required_reference_triggered_case_count": len(triggered),
        "required_reference_read_case_count": len(required_read),
        "reference_read_before_dependent_tool_case_count": len(ordered),
        "required_reference_read_rate": (
            _ratio(len(required_read), len(triggered)) if triggered else None
        ),
        "reference_read_before_dependent_tool_rate": (
            _ratio(len(ordered), len(triggered)) if triggered else None
        ),
        "reference_files": dict(sorted(files.items())),
    }


def _flatten_tool_calls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item:
                names.append(item)
            continue
        if isinstance(item, list):
            names.extend(str(name) for name in item if str(name or ""))
    return names


def _required_tool_path(tool_path: list[str]) -> list[str]:
    return [tool for tool in tool_path if not _is_optional_workflow_tool(tool)]


def _is_optional_workflow_tool(tool_name: str) -> bool:
    return tool_name in _OPTIONAL_WORKFLOW_TOOLS


def _split_search_phases(tool_path: list[str]) -> tuple[list[str], list[str]]:
    for index, tool in enumerate(tool_path):
        if tool == "knowledge_search":
            return tool_path[: index + 1], tool_path[index + 1 :]
    return tool_path, []


def _phase_workflow_result(expected: list[str], actual: list[str]) -> dict[str, Any]:
    expected_counter = Counter(expected)
    actual_counter = Counter(actual)
    called_required_count = sum(
        min(expected_count, actual_counter[tool])
        for tool, expected_count in expected_counter.items()
    )
    ordered_match_count = _subsequence_match_count(expected, actual)
    return {
        "required_tool_count": len(expected),
        "called_required_tool_count": called_required_count,
        "required_tool_recall": _ratio(called_required_count, len(expected)),
        "tool_order_score": _ratio(ordered_match_count, len(expected)),
        "missing_tools": _missing_tools(expected_counter, actual_counter),
    }


def _tool_call_records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    value = metadata.get("tool_call_records")
    if isinstance(value, list):
        records = [_normalized_tool_call_record(item) for item in value]
        return [record for record in records if record["name"]]
    value = metadata.get("tool_calls")
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            record = _normalized_tool_call_record(item)
            if record["name"]:
                records.append(record)
    return records


def _normalized_tool_call_record(value: Any) -> dict[str, Any]:
    payload = _model_or_mapping(value)
    return {
        "id": _optional_str(payload.get("id")),
        "name": _tool_call_name(payload),
        "arguments": _parse_arguments(payload.get("arguments")),
    }


def _tool_call_name(value: dict[str, Any]) -> str:
    return str(value.get("name") or value.get("tool") or value.get("tool_name") or "").strip()


def _tool_call_arguments(value: dict[str, Any]) -> dict[str, Any]:
    return _parse_arguments(value.get("arguments"))


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _match_tool_arguments(
    tool_name: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    for key in _hard_argument_keys(tool_name, expected):
        if key == "time_range":
            score = _time_range_overlap_score(expected.get(key), actual.get(key))
            if (score is None or score <= 0.0) and not _time_range_within_tolerance(
                expected.get(key),
                actual.get(key),
                tolerance_seconds=5.0,
            ):
                mismatches.append(
                    {"key": key, "expected": expected.get(key), "actual": actual.get(key)}
                )
            continue
        if not _argument_values_match(key, expected.get(key), actual.get(key)):
            mismatches.append(
                {"key": key, "expected": expected.get(key), "actual": actual.get(key)}
            )
    return {
        "matches": not mismatches,
        "mismatches": mismatches,
        "time_range_overlap_score": _time_range_overlap_score(
            expected.get("time_range"),
            actual.get("time_range"),
        )
        if "time_range" in expected
        else None,
    }


def _hard_argument_keys(tool_name: str, expected: dict[str, Any]) -> list[str]:
    keys_by_tool = {
        "knowledge_search": ("library_id",),
        "knowledge_discover": (),
        "query_refine": (),
        "video_inspect": ("library_id", "asset_id", "time_range"),
        "image_inspect": ("library_id", "asset_id"),
        "text_inspect": ("library_id", "asset_id"),
        "document_inspect": ("library_id", "asset_id"),
        "list_video_segments": ("library_id", "asset_id"),
        "read_file": ("file_path",),
        "grep": ("path",),
    }
    return [key for key in keys_by_tool.get(tool_name, ()) if key in expected]


def _target_scored_tool(tool_name: str) -> bool:
    return tool_name in {
        "knowledge_search",
        "text_inspect",
        "image_inspect",
        "document_inspect",
        "video_inspect",
    }


def _score_target_candidate(
    tool_name: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    hard_failures: list[dict[str, Any]] = []
    soft_issues: list[dict[str, Any]] = []
    score = 1.0
    for key in _target_hard_scalar_keys(tool_name, expected):
        if not _argument_values_match(key, expected.get(key), actual.get(key)):
            hard_failures.append(
                {"key": key, "expected": expected.get(key), "actual": actual.get(key)}
            )
    if tool_name == "video_inspect" and "time_range" in expected:
        time_score = _target_time_score(expected.get("time_range"), actual.get("time_range"))
        if time_score <= 0.0:
            hard_failures.append(
                {
                    "key": "time_range",
                    "expected": expected.get("time_range"),
                    "actual": actual.get("time_range"),
                }
            )
        else:
            score = min(score, time_score)
    if expected.get("evidence_id") and _argument_scalar(expected.get("evidence_id")) != (
        _argument_scalar(actual.get("evidence_id"))
    ):
        soft_issues.append(
            {
                "key": "evidence_id",
                "expected": expected.get("evidence_id"),
                "actual": _argument_scalar(actual.get("evidence_id")),
            }
        )
    return {
        "score": round(score, 6) if not hard_failures else 0.0,
        "hard_failures": hard_failures,
        "soft_issues": soft_issues,
        "time_range_overlap_score": _time_range_overlap_score(
            expected.get("time_range"),
            actual.get("time_range"),
        )
        if "time_range" in expected
        else None,
    }


def _target_hard_scalar_keys(tool_name: str, expected: dict[str, Any]) -> list[str]:
    keys_by_tool = {
        "knowledge_search": ("library_id",),
        "video_inspect": ("library_id", "asset_id"),
        "image_inspect": ("library_id", "asset_id"),
        "text_inspect": ("library_id", "asset_id"),
        "document_inspect": ("library_id", "asset_id"),
    }
    return [key for key in keys_by_tool.get(tool_name, ()) if key in expected]


def _target_time_score(expected: Any, actual: Any) -> float:
    if _time_range_within_tolerance(expected, actual, tolerance_seconds=5.0):
        return 1.0
    overlap_score = _time_range_overlap_score(expected, actual)
    return overlap_score or 0.0


def _fewest_hard_failures(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    best = min(candidates, key=lambda item: len(item.get("hard_failures") or []))
    return list(best.get("hard_failures") or [])


def _argument_failure_buckets(mismatches: list[dict[str, Any]]) -> list[str]:
    buckets: list[str] = []
    keys = {str(item.get("key") or "") for item in mismatches}
    if "time_range" in keys:
        buckets.append("wrong_time_range")
    if keys & {"library_id", "asset_id", "evidence_id", "chunk_id", "file_path", "path"}:
        buckets.append("wrong_tool_target")
    if keys - {
        "time_range",
        "library_id",
        "asset_id",
        "evidence_id",
        "chunk_id",
        "file_path",
        "path",
    }:
        buckets.append("wrong_tool_arguments")
    if not buckets and mismatches:
        buckets.append("wrong_tool_arguments")
    return buckets


def _argument_values_match(key: str, expected: Any, actual: Any) -> bool:
    if (
        key in {"file_path", "path"}
        and _is_large_tool_result_path(expected)
        and _is_large_tool_result_path(actual)
    ):
        return True
    return _argument_scalar(expected) == _argument_scalar(actual)


def _is_large_tool_result_path(value: Any) -> bool:
    return "/large_tool_results/" in _argument_scalar(value)


def _argument_scalar(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _time_range_overlap_score(expected: Any, actual: Any) -> float | None:
    expected_range = _time_range_bounds(expected)
    actual_range = _time_range_bounds(actual)
    if expected_range is None or actual_range is None:
        return None
    expected_start, expected_end = expected_range
    actual_start, actual_end = actual_range
    gold_duration = expected_end - expected_start
    if gold_duration <= 0:
        return 0.0
    overlap = max(0.0, min(expected_end, actual_end) - max(expected_start, actual_start))
    return round(overlap / gold_duration, 6)


def _time_range_within_tolerance(
    expected: Any,
    actual: Any,
    *,
    tolerance_seconds: float,
) -> bool:
    expected_range = _time_range_bounds(expected)
    actual_range = _time_range_bounds(actual)
    if expected_range is None or actual_range is None:
        return False
    return (
        abs(expected_range[0] - actual_range[0]) <= tolerance_seconds
        and abs(expected_range[1] - actual_range[1]) <= tolerance_seconds
    )


def _time_range_bounds(value: Any) -> tuple[float, float] | None:
    payload = _model_or_mapping(value)
    start = _optional_float(payload.get("start_seconds"))
    end = _optional_float(payload.get("end_seconds"))
    if start is None or end is None:
        return None
    return (start, end)


def _subsequence_match_count(expected: list[str], actual: list[str]) -> int:
    if not expected:
        return 0
    actual_index = 0
    matched = 0
    for expected_tool in expected:
        while actual_index < len(actual) and actual[actual_index] != expected_tool:
            actual_index += 1
        if actual_index >= len(actual):
            continue
        matched += 1
        actual_index += 1
    return matched


def _missing_tools(expected: Counter[str], actual: Counter[str]) -> list[str]:
    missing: list[str] = []
    for tool, count in expected.items():
        missing.extend([tool] * max(0, count - actual[tool]))
    return missing


def _modalities_from_tools(tool_path: list[str]) -> set[str]:
    modalities: set[str] = set()
    for tool in tool_path:
        if tool in {"video_inspect", "list_video_segments", "evidence_asr"}:
            modalities.add("video")
        if tool in {"text_inspect", "read_file", "knowledge_search", "grep"}:
            modalities.add("text")
        if tool in {"image_inspect", "document_inspect", "evidence_ocr", "image_understand"}:
            modalities.add("image")
    return modalities


def _normalize_modality(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"document", "doc"}:
        return "text"
    return normalized


def _answer_markers(answer: str) -> list[str]:
    return [match.group(0) for match in _CITATION_MARKER_RE.finditer(answer)]


def _reference_overlap_recall(reference_answer: str, final_answer: str) -> float | None:
    reference_units = _text_bigrams(reference_answer)
    if not reference_units:
        return None
    answer_units = _text_bigrams(final_answer)
    if not answer_units:
        return 0.0
    return _ratio(len(reference_units & answer_units), len(reference_units))


def _structured_reference_assessment(
    *,
    reference_answer: str,
    final_answer: str,
    answer_format: str,
    question: str = "",
    answer_disposition: AnswerDisposition | None = None,
    refusal_judge: RefusalJudge | None = None,
) -> dict[str, Any] | None:
    """Conservatively compare a parseable final conclusion with a hidden reference.

    This intentionally complements rather than replaces the lexical diagnostic. A
    result only becomes verdict-bearing when both the declared answer format and
    the model's final conclusion can be parsed without guessing from arbitrary
    numbers or prose in the reasoning body.
    """

    reference = reference_answer.strip()
    if not reference:
        return None
    normalized_format = answer_format.strip() or "Str"
    conclusion = _answer_conclusion(final_answer)
    if _is_unanswerable_reference(reference):
        if answer_disposition is not None:
            matched = answer_disposition.kind == "unanswerable"
            return {
                "answer_format": normalized_format,
                "kind": "unanswerable",
                "evaluable": True,
                "matched": matched,
                "reference_value": reference,
                "predicted_value": answer_disposition.kind,
                "reason_code": (
                    "agent_declared_unanswerable" if matched else "agent_declared_answered"
                ),
                "decision_source": "agent",
                "disposition_reason": answer_disposition.reason,
            }
        if refusal_judge is None:
            return {
                "answer_format": normalized_format,
                "kind": "unanswerable",
                "evaluable": False,
                "matched": None,
                "reference_value": reference,
                "predicted_value": conclusion,
                "reason_code": "refusal_judge_required",
                "decision_source": "unresolved",
            }
        try:
            decision = refusal_judge.judge_refusal(
                RefusalJudgeRequest(question=question, answer=final_answer)
            )
        except Exception as exc:
            return {
                "answer_format": normalized_format,
                "kind": "unanswerable",
                "evaluable": False,
                "matched": None,
                "reference_value": reference,
                "predicted_value": conclusion,
                "reason_code": "refusal_judge_failed",
                "decision_source": "unresolved",
                "judge_error_type": type(exc).__name__,
            }
        matched = decision.is_refusal
        return {
            "answer_format": normalized_format,
            "kind": "unanswerable",
            "evaluable": True,
            "matched": matched,
            "reference_value": reference,
            "predicted_value": conclusion,
            "reason_code": ("llm_judged_unanswerable" if matched else "llm_judged_answered"),
            "decision_source": "llm",
            "judge_reason": decision.reason,
        }

    format_key = normalized_format.lower()
    if format_key in {"float", "int", "integer", "number"}:
        reference_value = _single_number(reference)
        predicted_value = _conclusion_number(conclusion)
        if reference_value is None or predicted_value is None:
            return {
                "answer_format": normalized_format,
                "kind": "number",
                "evaluable": False,
                "matched": None,
                "reference_value": reference_value,
                "predicted_value": predicted_value,
                "reason_code": "numeric_conclusion_unparseable",
            }
        matched = math.isclose(reference_value, predicted_value, rel_tol=1e-6, abs_tol=1e-9)
        return {
            "answer_format": normalized_format,
            "kind": "number",
            "evaluable": True,
            "matched": matched,
            "reference_value": reference_value,
            "predicted_value": predicted_value,
            "reason_code": "numeric_match" if matched else "numeric_mismatch",
        }

    if format_key == "list":
        reference_items = _literal_string_list(reference)
        predicted_items = _answer_list_items(conclusion)
        if reference_items is None or predicted_items is None:
            return {
                "answer_format": normalized_format,
                "kind": "list",
                "evaluable": False,
                "matched": None,
                "reference_items": reference_items,
                "predicted_items": predicted_items,
                "reason_code": "list_conclusion_unparseable",
            }
        normalized_reference = {_normalize_answer_item(item): item for item in reference_items}
        normalized_predicted = {_normalize_answer_item(item): item for item in predicted_items}
        covered = {
            reference_key
            for reference_key in normalized_reference
            if any(reference_key in predicted_key for predicted_key in normalized_predicted)
        }
        extras = {
            predicted_key
            for predicted_key in normalized_predicted
            if not any(reference_key in predicted_key for reference_key in normalized_reference)
        }
        coverage_recall = _ratio(len(covered), len(normalized_reference))
        matched = coverage_recall == 1.0 and not extras
        return {
            "answer_format": normalized_format,
            "kind": "list",
            "evaluable": True,
            "matched": matched,
            "reference_items": reference_items,
            "predicted_items": predicted_items,
            "coverage_recall": coverage_recall,
            "extra_items": [normalized_predicted[item] for item in sorted(extras)],
            "reason_code": "list_match" if matched else "list_mismatch",
        }

    normalized_reference = _normalize_answer_item(reference)
    normalized_conclusion = _normalize_answer_item(conclusion)
    if not normalized_conclusion:
        return {
            "answer_format": normalized_format,
            "kind": "text",
            "evaluable": False,
            "matched": None,
            "reference_value": reference,
            "predicted_value": conclusion,
            "reason_code": "text_conclusion_unparseable",
        }
    matched = normalized_reference in _normalize_answer_item(final_answer)
    return {
        "answer_format": normalized_format,
        "kind": "text",
        "evaluable": matched,
        "matched": True if matched else None,
        "reference_value": reference,
        "predicted_value": conclusion,
        "reason_code": "text_match" if matched else "semantic_text_judge_required",
    }


def _answer_conclusion(answer: str) -> str:
    without_citations = _CITATION_MARKER_RE.sub("", answer).strip()
    marker_matches = list(
        re.finditer(
            r"(?im)(?:^|\n|\*\*)\s*(?:final\s+answer|answer|最终答案|答案)\s*[:：]\s*",
            without_citations,
        )
    )
    if marker_matches:
        remainder = without_citations[marker_matches[-1].end() :]
        lines = [
            stripped
            for line in remainder.splitlines()
            if (stripped := line.strip()) and stripped.strip("*_`~ ")
        ]
        conclusion = lines[0] if lines and not _looks_like_list_item(lines[0]) else remainder
    else:
        nonempty_lines = [line.strip() for line in without_citations.splitlines() if line.strip()]
        conclusion = nonempty_lines[-1] if nonempty_lines else ""
    return conclusion.strip().strip("*_` ")


def _looks_like_list_item(value: str) -> bool:
    return re.match(r"\s*(?:[-*•]|\d+[.)])\s+", value) is not None


def _single_number(value: str) -> float | None:
    matches = re.findall(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value)
    if len(matches) != 1:
        return None
    return float(matches[0].replace(",", ""))


def _conclusion_number(value: str) -> float | None:
    """Extract one declared numeric result without guessing from calculation inputs."""

    direct = _single_number(value)
    if direct is not None:
        return direct
    emphasized = re.findall(
        r"\*\*\s*((?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?)\s*(?:\*\*|$)",
        value,
    )
    parsed = {float(item.removesuffix("%").replace(",", "")) for item in emphasized}
    if len(parsed) != 1:
        return None
    return next(iter(parsed))


def _literal_string_list(value: str) -> list[str] | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return [item.strip() for item in parsed if item.strip()]


def _answer_list_items(value: str) -> list[str] | None:
    literal = _literal_string_list(value)
    if literal is not None:
        return literal
    items: list[str] = []
    for line in value.splitlines():
        match = re.match(r"\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", line)
        if match:
            item = match.group(1).strip().strip("*_` ")
            if item:
                items.append(item)
    return items or None


def _normalize_answer_item(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_unanswerable_reference(value: str) -> bool:
    return _normalize_answer_item(value) in {"notanswerable", "unanswerable", "无法回答"}


def _text_bigrams(value: str) -> set[str]:
    compact = "".join(char for char in value.lower() if char.isalnum())
    if len(compact) < 2:
        return set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _citation_has_valid_locator(citation: dict[str, Any]) -> bool:
    source = citation.get("source")
    source = source if isinstance(source, dict) else {}
    source_type = str(citation.get("type") or source.get("type") or "")
    if source_type == "video":
        time_range = source.get("time_range")
        if not isinstance(time_range, dict):
            return False
        return (
            _optional_float(time_range.get("start_seconds")) is not None
            and _optional_float(time_range.get("end_seconds")) is not None
            and bool(source.get("document_id") or source.get("asset_id"))
        )
    if source_type == "image":
        return bool(source.get("chunk_id") or source.get("source_image_id"))
    return bool(source.get("chunk_id"))


def _candidate_tool_payloads(trace: dict[str, Any]) -> list[dict[str, Any]]:
    context_payloads = [
        payload
        for payload in _trace_tool_payloads(trace)
        if str(payload.get("tool") or "") in {"knowledge_search", "list_video_segments"}
    ]
    metadata = _model_or_mapping(trace.get("metadata"))
    projected_searches = [
        {
            "tool": "knowledge_search",
            "library_id": payload.get("library_id"),
            "evidence": payload.get("candidates") or [],
        }
        for payload in metadata.get("search_result_summaries") or []
        if isinstance(payload, dict)
    ]
    if not projected_searches:
        return context_payloads
    return projected_searches + [
        payload
        for payload in context_payloads
        if str(payload.get("tool") or "") == "list_video_segments"
    ]


def _candidate_tool_events(trace: dict[str, Any]) -> list[dict[str, Any]]:
    from agents.runtime.core.tools.base import unwrap_tool_data

    events: list[dict[str, Any]] = []
    candidate_calls: dict[str, str] = {}
    offload_calls: dict[str, str] = {}
    offload_read_calls: dict[str, str] = {}
    for index, message in enumerate(trace.get("context") or []):
        message_payload = _model_or_mapping(message)
        payload = unwrap_tool_data(_json_object_prefix(message_payload.get("content")))
        if str(payload.get("tool") or "") in {"knowledge_search", "list_video_segments"}:
            events.append({"index": index, "source_tool": str(payload["tool"]), "payload": payload})

        content = str(message_payload.get("content") or "")
        tool_call_id = str(message_payload.get("tool_call_id") or "")
        if tool_call_id in candidate_calls and "/large_tool_results/" in content:
            offload_calls[tool_call_id] = candidate_calls[tool_call_id]

        for tool_call in message_payload.get("tool_calls") or []:
            call_payload = _model_or_mapping(tool_call)
            function = _model_or_mapping(call_payload.get("function"))
            tool_name = str(function.get("name") or "")
            call_id = str(call_payload.get("id") or "")
            if tool_name in {"knowledge_search", "list_video_segments"} and call_id:
                candidate_calls[call_id] = tool_name
                continue
            if tool_name not in {"read_file", "grep"}:
                continue
            arguments = _json_mapping(function.get("arguments"))
            argument_text = json.dumps(arguments, ensure_ascii=False)
            source_tool = next(
                (
                    tool
                    for candidate_id, tool in offload_calls.items()
                    if candidate_id in argument_text
                ),
                None,
            )
            if source_tool is None:
                continue
            if call_id:
                offload_read_calls[call_id] = source_tool

        if tool_call_id in offload_read_calls and content:
            events.append(
                {
                    "index": index,
                    "source_tool": offload_read_calls[tool_call_id],
                    "text": content,
                }
            )
    if not any(event.get("source_tool") == "knowledge_search" for event in events):
        metadata = _model_or_mapping(trace.get("metadata"))
        tool_call_records = [
            _model_or_mapping(record)
            for record in metadata.get("tool_call_records") or []
            if isinstance(record, dict)
        ]
        record_indexes = {
            str(record.get("id") or ""): index
            for index, record in enumerate(tool_call_records)
            if str(record.get("id") or "")
        }
        events.extend(
            {
                "index": record_indexes.get(str(payload.get("tool_call_id") or "")),
                "source_tool": "knowledge_search",
                "payload": {
                    "tool": "knowledge_search",
                    "library_id": payload.get("library_id"),
                    "evidence": payload.get("candidates") or [],
                },
            }
            for payload in metadata.get("search_result_summaries") or []
            if isinstance(payload, dict)
        )
    return events


def _ordered_inspect_observations(
    trace: dict[str, Any],
    actual_tool_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from agents.runtime.core.tools.base import unwrap_tool_data

    metadata = _model_or_mapping(trace.get("metadata"))
    projected = [
        _model_or_mapping(item)
        for item in metadata.get("inspect_result_summaries") or []
        if isinstance(item, dict)
    ]
    if projected:
        record_indexes = {
            str(record.get("id") or ""): index
            for index, record in enumerate(actual_tool_records)
            if str(record.get("id") or "")
        }
        return [
            {
                "tool": str(item.get("tool") or ""),
                "arguments": _model_or_mapping(item.get("locator")),
                "result": {
                    key: item.get(key)
                    for key in ("success", "claims", "source", "error")
                    if item.get(key) is not None
                },
                "call_index": record_indexes.get(str(item.get("tool_call_id") or "")),
                "result_index": None,
            }
            for item in projected
        ]
    pending: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    for index, message in enumerate(trace.get("context") or []):
        message_payload = _model_or_mapping(message)
        for tool_call in message_payload.get("tool_calls") or []:
            call_payload = _model_or_mapping(tool_call)
            function = _model_or_mapping(call_payload.get("function"))
            tool_name = str(function.get("name") or "")
            call_id = str(call_payload.get("id") or "")
            if not call_id or not tool_name.endswith("_inspect"):
                continue
            pending[call_id] = {
                "tool": tool_name,
                "arguments": _json_mapping(function.get("arguments")),
                "call_index": index,
            }

        call_id = str(message_payload.get("tool_call_id") or "")
        call = pending.get(call_id)
        if call is None:
            continue
        content_payload = unwrap_tool_data(_json_object_prefix(message_payload.get("content")))
        eval_message = _model_or_mapping(message_payload.get("eval_message"))
        result = eval_message or _model_or_mapping(content_payload.get("result"))
        observations.append({**call, "result_index": index, "result": result})

    if observations:
        return observations
    return _inspect_observations(trace, actual_tool_records)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _used_final_citations(trace: dict[str, Any], final_answer: str) -> list[dict[str, Any]]:
    metadata = _model_or_mapping(trace.get("metadata"))
    citations = metadata.get("citations")
    citation_map = citations if isinstance(citations, dict) else {}
    used_markers = set(_answer_markers(final_answer))
    used: list[dict[str, Any]] = []
    for key, citation in citation_map.items():
        citation_payload = _model_or_mapping(citation)
        marker = str(citation_payload.get("marker") or f"[{key}]").strip()
        if marker not in used_markers:
            continue
        used.append({**citation_payload, "marker": marker})
    return used


def _inspect_funnel_item(
    gold: dict[str, Any],
    candidate_events: list[dict[str, Any]],
    inspect_observations: list[dict[str, Any]],
    final_citations: list[dict[str, Any]],
) -> dict[str, Any]:
    matching_candidate_events = [
        event for event in candidate_events if _candidate_event_matches_gold(event, gold)
    ]
    search_candidate_events = [
        event
        for event in matching_candidate_events
        if event.get("source_tool") == "knowledge_search"
    ]
    list_candidate_events = [
        event
        for event in matching_candidate_events
        if event.get("source_tool") == "list_video_segments"
    ]
    first_list_index = min(
        (event["index"] for event in list_candidate_events if isinstance(event.get("index"), int)),
        default=None,
    )
    search_before_list = bool(
        first_list_index is not None
        and any(
            isinstance(event.get("index"), int) and event["index"] < first_list_index
            for event in search_candidate_events
        )
    )
    list_incremental_exposed = bool(list_candidate_events) and not search_before_list
    targeted_observations = [
        observation
        for observation in inspect_observations
        if _inspect_observation_matches_gold(observation, gold)
    ]
    after_exposure = [
        observation
        for observation in targeted_observations
        if _observation_follows_candidate(observation, matching_candidate_events)
    ]
    eligible = [
        (observation, claims)
        for observation in after_exposure
        if (claims := _eligible_inspect_claims(observation))
    ]
    aligned = [
        (observation, claim)
        for observation, claims in eligible
        for claim in claims
        if _inspect_claim_matches_gold(claim, observation, gold)
    ]
    eligible_claims = [(observation, claim) for observation, claims in eligible for claim in claims]
    claim_sources = [
        _inspect_claim_source(claim, observation) for observation, claim in eligible_claims
    ]
    matching_final = [
        citation for citation in final_citations if _final_citation_matches_gold(citation, gold)
    ]
    used_markers = {
        str(citation.get("marker") or "").strip()
        for citation in final_citations
        if str(citation.get("marker") or "").strip()
    }
    used_claim_sources = [
        _inspect_claim_source(claim, observation)
        for observation, claim in eligible_claims
        if str(claim.get("agent_citation") or "").strip() in used_markers
    ]
    claim_coverage = _video_source_coverage_metrics(claim_sources, gold)
    used_claim_coverage = _video_source_coverage_metrics(used_claim_sources, gold)
    aligned_markers = {
        str(claim.get("agent_citation") or "").strip()
        for _, claim in aligned
        if str(claim.get("agent_citation") or "").strip()
    }
    linked_final = [
        citation
        for citation in matching_final
        if str(citation.get("marker") or "").strip() in aligned_markers
        and str(citation.get("tool") or "") == f"{_gold_modality(gold)}_inspect"
    ]
    return {
        "gold_id": str(gold.get("gold_id") or ""),
        "modality": _gold_modality(gold),
        "workspace": str(gold.get("workspace") or ""),
        "asset_id": str(gold.get("asset_id") or ""),
        "candidate_exposed": bool(matching_candidate_events),
        "search_candidate_exposed": bool(search_candidate_events),
        "list_candidate_exposed": bool(list_candidate_events),
        "list_incremental_exposed": list_incremental_exposed,
        "search_candidate_inspected": bool(
            targeted_observations
            and any(
                _observation_follows_candidate(observation, search_candidate_events)
                for observation in targeted_observations
            )
        ),
        "list_candidate_inspected": bool(
            targeted_observations
            and any(
                _observation_follows_candidate(observation, list_candidate_events)
                for observation in targeted_observations
            )
        ),
        "list_incremental_candidate_inspected": bool(
            list_incremental_exposed
            and any(
                _observation_follows_candidate(observation, list_candidate_events)
                for observation in targeted_observations
            )
        ),
        "inspect_targeted": bool(targeted_observations),
        "inspect_after_exposure": bool(after_exposure),
        "inspect_return_eligible": bool(eligible),
        "inspect_return_aligned": bool(aligned),
        "claim_locator_hit_count": claim_coverage["locator_hit_count"],
        "gold_with_claim_locator_hit": claim_coverage["gold_with_locator_hit"],
        "claim_union_coverage_ratio": claim_coverage["union_coverage_ratio"],
        "claim_union_largest_gap_seconds": claim_coverage["union_largest_gap_seconds"],
        "claim_union_covered": claim_coverage["union_covered"],
        "single_claim_aligned": claim_coverage["single_source_aligned"],
        "final_citation_matched": bool(matching_final),
        "used_claim_count": used_claim_coverage["source_count"],
        "used_claim_locator_hit_count": used_claim_coverage["locator_hit_count"],
        "gold_with_used_claim_locator_hit": used_claim_coverage["gold_with_locator_hit"],
        "used_claim_union_coverage_ratio": used_claim_coverage["union_coverage_ratio"],
        "used_claim_union_largest_gap_seconds": used_claim_coverage["union_largest_gap_seconds"],
        "used_claim_union_covered": used_claim_coverage["union_covered"],
        "used_single_claim_aligned": used_claim_coverage["single_source_aligned"],
        "inspect_chain_completed": bool(linked_final),
        "aligned_inspect_markers": sorted(aligned_markers),
        "matched_final_markers": sorted(
            str(citation.get("marker") or "") for citation in matching_final
        ),
    }


def _inspect_funnel_group(
    requirement_id: str,
    alternatives: list[dict[str, Any]],
    candidate_events: list[dict[str, Any]],
    inspect_observations: list[dict[str, Any]],
    final_citations: list[dict[str, Any]],
) -> dict[str, Any]:
    items = [
        _inspect_funnel_item(gold, candidate_events, inspect_observations, final_citations)
        for gold in alternatives
    ]
    selected = max(
        items,
        key=lambda item: (
            bool(item["final_citation_matched"]),
            bool(item["inspect_chain_completed"]),
            bool(item["inspect_return_aligned"]),
            bool(item["inspect_targeted"]),
            bool(item["candidate_exposed"]),
        ),
    )
    return {
        **selected,
        "gold_id": requirement_id,
        "alternative_gold_ids": [str(gold.get("gold_id") or "") for gold in alternatives],
        "alternative_count": len(alternatives),
    }


def _candidate_event_matches_gold(event: dict[str, Any], gold: dict[str, Any]) -> bool:
    payload = _model_or_mapping(event.get("payload"))
    if payload and _candidate_payload_matches_gold(payload, gold):
        return True
    text = str(event.get("text") or "")
    if not text:
        return False
    locators = [
        _argument_scalar(gold.get("chunk_id")),
        *(_argument_scalar(item) for item in gold.get("segment_ids") or []),
    ]
    return any(locator and locator in text for locator in locators)


def _observation_follows_candidate(
    observation: dict[str, Any], candidate_events: list[dict[str, Any]]
) -> bool:
    call_index = observation.get("call_index")
    if not isinstance(call_index, int):
        return False
    return any(
        isinstance(event.get("index"), int) and event["index"] < call_index
        for event in candidate_events
    )


def _eligible_inspect_claims(observation: dict[str, Any]) -> list[dict[str, Any]]:
    result = _model_or_mapping(observation.get("result"))
    if result.get("success") is False:
        return []
    tool_name = str(observation.get("tool") or "")
    claims = result.get("claims")
    if not isinstance(claims, list):
        return []
    eligible: list[dict[str, Any]] = []
    for claim in claims:
        claim_payload = _model_or_mapping(claim)
        confidence = _optional_float(claim_payload.get("confidence"))
        if confidence is not None and confidence <= 0.0:
            continue
        if tool_name == "text_inspect" and claim_payload.get("quote_verified") is False:
            continue
        if not str(claim_payload.get("agent_citation") or "").strip():
            continue
        eligible.append(claim_payload)
    return eligible


def _inspect_claim_matches_gold(
    claim: dict[str, Any], observation: dict[str, Any], gold: dict[str, Any]
) -> bool:
    result = _model_or_mapping(observation.get("result"))
    source = _model_or_mapping(claim.get("source")) or _model_or_mapping(result.get("source"))
    return _source_matches_gold(source, gold, strict_time=True)


def _inspect_claim_source(claim: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    result = _model_or_mapping(observation.get("result"))
    return _model_or_mapping(claim.get("source")) or _model_or_mapping(result.get("source"))


def _video_source_coverage_metrics(
    sources: list[dict[str, Any]], gold: dict[str, Any]
) -> dict[str, Any]:
    if _gold_modality(gold) != "video":
        return {
            "source_count": len(sources),
            "locator_hit_count": 0,
            "gold_with_locator_hit": False,
            "union_coverage_ratio": None,
            "union_largest_gap_seconds": None,
            "union_covered": False,
            "single_source_aligned": False,
        }
    gold_range = _time_range_bounds(gold.get("time_range"))
    if gold_range is None:
        return {
            "source_count": len(sources),
            "locator_hit_count": 0,
            "gold_with_locator_hit": False,
            "union_coverage_ratio": None,
            "union_largest_gap_seconds": None,
            "union_covered": False,
            "single_source_aligned": False,
        }
    gold_asset = _argument_scalar(gold.get("asset_id"))
    hit_ranges: list[tuple[float, float]] = []
    single_source_aligned = False
    for source in sources:
        source_asset = _argument_scalar(source.get("document_id") or source.get("asset_id"))
        if gold_asset and source_asset != gold_asset:
            continue
        source_range_value = source.get("absolute_time_range") or source.get("time_range")
        source_range = _time_range_bounds(source_range_value)
        if source_range is None:
            continue
        single_source_aligned = single_source_aligned or _time_range_within_tolerance(
            gold.get("time_range"), source_range_value, tolerance_seconds=5.0
        )
        overlap = max(
            0.0,
            min(gold_range[1], source_range[1]) - max(gold_range[0], source_range[0]),
        )
        source_duration = source_range[1] - source_range[0]
        if source_duration <= 0 or overlap <= 0 or overlap / source_duration <= 0.5:
            continue
        hit_ranges.append(
            (max(gold_range[0], source_range[0]), min(gold_range[1], source_range[1]))
        )
    merged = _merge_time_ranges(hit_ranges)
    covered_duration = sum(end - start for start, end in merged)
    gold_duration = gold_range[1] - gold_range[0]
    coverage_ratio = round(covered_duration / gold_duration, 6) if gold_duration > 0 else 0.0
    largest_gap = _largest_uncovered_gap(gold_range, merged)
    return {
        "source_count": len(sources),
        "locator_hit_count": len(hit_ranges),
        "gold_with_locator_hit": bool(hit_ranges),
        "union_coverage_ratio": coverage_ratio,
        "union_largest_gap_seconds": round(largest_gap, 6),
        "union_covered": coverage_ratio >= 0.8 and largest_gap <= 5.0,
        "single_source_aligned": single_source_aligned,
    }


def _merge_time_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _largest_uncovered_gap(
    gold_range: tuple[float, float], covered: list[tuple[float, float]]
) -> float:
    cursor = gold_range[0]
    largest = 0.0
    for start, end in covered:
        largest = max(largest, start - cursor)
        cursor = max(cursor, end)
    return max(largest, gold_range[1] - cursor)


def _final_citation_matches_gold(citation: dict[str, Any], gold: dict[str, Any]) -> bool:
    source = _model_or_mapping(citation.get("source"))
    return _source_matches_gold(source, gold, strict_time=True)


def _source_matches_gold(
    source: dict[str, Any], gold: dict[str, Any], *, strict_time: bool
) -> bool:
    gold_asset = _argument_scalar(gold.get("asset_id"))
    source_asset = _argument_scalar(source.get("document_id") or source.get("asset_id"))
    if gold_asset and source_asset != gold_asset:
        return False
    modality = _gold_modality(gold)
    if modality == "video":
        source_range = source.get("absolute_time_range") or source.get("time_range")
        if strict_time:
            return _time_range_within_tolerance(
                gold.get("time_range"), source_range, tolerance_seconds=5.0
            )
        return (_time_range_overlap_score(gold.get("time_range"), source_range) or 0.0) > 0.0
    if modality == "text":
        gold_chunk = _argument_scalar(gold.get("chunk_id"))
        return not gold_chunk or gold_chunk == _argument_scalar(source.get("chunk_id"))
    if modality == "image":
        gold_chunk = _argument_scalar(gold.get("chunk_id"))
        source_id = _argument_scalar(source.get("chunk_id") or source.get("source_image_id"))
        return not gold_chunk or gold_chunk == source_id
    return bool(source_asset)


def _inspect_observations(
    trace: dict[str, Any],
    actual_tool_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inspect_results = [
        payload
        for payload in _trace_tool_payloads(trace)
        if str(payload.get("tool") or "").endswith("_inspect")
    ]
    result_cursor = 0
    observations: list[dict[str, Any]] = []
    for record_index, record in enumerate(actual_tool_records):
        tool_name = _tool_call_name(record)
        if not tool_name.endswith("_inspect"):
            continue
        result_payload: dict[str, Any] = {}
        for index in range(result_cursor, len(inspect_results)):
            candidate = inspect_results[index]
            if str(candidate.get("tool") or "") == tool_name:
                result_payload = candidate
                result_cursor = index + 1
                break
        observations.append(
            {
                "tool": tool_name,
                "arguments": _tool_call_arguments(record),
                "result": _model_or_mapping(result_payload.get("result")),
                "call_index": record_index,
                "result_index": None,
            }
        )
    return observations


def _gold_funnel_item(
    gold: dict[str, Any],
    candidate_payloads: list[dict[str, Any]],
    inspect_observations: list[dict[str, Any]],
) -> dict[str, Any]:
    inspect_matches = [
        observation
        for observation in inspect_observations
        if _inspect_observation_matches_gold(observation, gold)
    ]
    inspected = bool(inspect_matches)
    inspect_markers = [
        marker
        for observation in inspect_matches
        for marker in _eligible_inspect_markers(observation)
    ]
    return {
        "gold_id": str(gold.get("gold_id") or ""),
        "modality": _gold_modality(gold),
        "workspace": str(gold.get("workspace") or ""),
        "asset_id": str(gold.get("asset_id") or ""),
        "candidate_exposed": any(
            _candidate_payload_matches_gold(payload, gold) for payload in candidate_payloads
        ),
        "inspected": inspected,
        "inspect_locator_hit": bool(inspect_markers),
        "inspect_markers": inspect_markers,
    }


def _candidate_payload_matches_gold(payload: dict[str, Any], gold: dict[str, Any]) -> bool:
    from agents.runtime.core.tools.base import unwrap_tool_data

    payload = unwrap_tool_data(payload)
    workspace = str(gold.get("workspace") or "")
    library_id = str(payload.get("library_id") or "")
    if workspace and library_id and workspace != library_id:
        return False
    for evidence in _candidate_evidence_items(payload):
        if _evidence_matches_gold(evidence, gold):
            return True
    return False


def _candidate_evidence_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    from agents.runtime.core.tools.base import unwrap_tool_data

    payload = unwrap_tool_data(payload)
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    segments = payload.get("segments") or payload.get("video_segments") or payload.get("results")
    if isinstance(segments, list):
        return [item for item in segments if isinstance(item, dict)]
    return []


def _evidence_matches_gold(evidence: dict[str, Any], gold: dict[str, Any]) -> bool:
    metadata = _model_or_mapping(evidence.get("metadata"))
    evidence_asset = evidence.get("asset_id") or metadata.get("asset_id")
    if evidence_asset and _argument_scalar(evidence_asset) != _argument_scalar(
        gold.get("asset_id")
    ):
        return False
    modality = _gold_modality(gold)
    if modality == "video":
        gold_range = gold.get("time_range")
        ranges = _evidence_video_ranges(evidence)
        if not ranges:
            return True
        return any((_time_range_overlap_score(gold_range, value) or 0.0) > 0.0 for value in ranges)
    if modality == "text":
        chunk_id = _argument_scalar(gold.get("chunk_id"))
        if chunk_id and chunk_id == _argument_scalar(evidence.get("id")):
            return True
        gold_segments = {_argument_scalar(item) for item in gold.get("segment_ids") or []}
        evidence_segments = _evidence_segment_ids(evidence)
        return bool(gold_segments and gold_segments <= evidence_segments)
    return True


def _evidence_video_ranges(evidence: dict[str, Any]) -> list[Any]:
    ranges: list[Any] = []
    if isinstance(evidence.get("time_range"), dict):
        ranges.append(evidence["time_range"])
    for part in evidence.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if str(part.get("kind") or "") == "video" and isinstance(part.get("time_range"), dict):
            ranges.append(part["time_range"])
    metadata = _model_or_mapping(evidence.get("metadata"))
    begin_ts = _optional_float(metadata.get("begin_ts"))
    end_ts = _optional_float(metadata.get("end_ts"))
    if begin_ts is not None and end_ts is not None:
        ranges.append({"start_seconds": begin_ts, "end_seconds": end_ts})
    return ranges


def _evidence_segment_ids(evidence: dict[str, Any]) -> set[str]:
    ids = {_argument_scalar(evidence.get("segment_id"))}
    for key in ("segment_ids", "segments"):
        value = evidence.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    ids.add(_argument_scalar(item.get("segment_id") or item.get("id")))
                else:
                    ids.add(_argument_scalar(item))
    return {item for item in ids if item}


def _inspect_observation_matches_gold(observation: dict[str, Any], gold: dict[str, Any]) -> bool:
    tool_name = str(observation.get("tool") or "")
    modality = _gold_modality(gold)
    if modality and tool_name != f"{modality}_inspect":
        return False
    arguments = _model_or_mapping(observation.get("arguments"))
    workspace = _argument_scalar(gold.get("workspace"))
    if workspace and workspace != _argument_scalar(arguments.get("library_id")):
        return False
    if _argument_scalar(gold.get("asset_id")) != _argument_scalar(arguments.get("asset_id")):
        return False
    if modality == "video":
        return _target_time_score(gold.get("time_range"), arguments.get("time_range")) > 0.0
    if modality == "text":
        chunk_id = _argument_scalar(gold.get("chunk_id"))
        evidence_id = _argument_scalar(arguments.get("evidence_id") or arguments.get("chunk_id"))
        if chunk_id and evidence_id and chunk_id != evidence_id:
            return False
    return True


def _eligible_inspect_markers(observation: dict[str, Any]) -> list[str]:
    result = _model_or_mapping(observation.get("result"))
    if result.get("success") is False:
        return []
    markers: list[str] = []
    tool_name = str(observation.get("tool") or "")
    claims = result.get("claims")
    if not isinstance(claims, list):
        return markers
    for claim in claims:
        claim_payload = _model_or_mapping(claim)
        confidence = _optional_float(claim_payload.get("confidence"))
        if confidence is not None and confidence <= 0.0:
            continue
        if tool_name == "text_inspect" and claim_payload.get("quote_verified") is False:
            continue
        marker = str(claim_payload.get("agent_citation") or "").strip()
        if marker:
            markers.append(marker)
    return markers


def _trace_tool_payloads(trace: dict[str, Any]) -> list[dict[str, Any]]:
    from agents.runtime.core.tools.base import unwrap_tool_data

    payloads: list[dict[str, Any]] = []
    for message in trace.get("context") or []:
        message_payload = _model_or_mapping(message)
        content = message_payload.get("content")
        payload = unwrap_tool_data(_json_object_prefix(content))
        if payload:
            payloads.append(payload)
    return payloads


def _json_object_prefix(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _gold_modality(gold: dict[str, Any]) -> str:
    modality = str(gold.get("modality") or "").strip().lower()
    if modality in {"document", "doc"}:
        return "text"
    return modality


def _touched_assets(trace: dict[str, Any]) -> set[str]:
    touched: set[str] = set()
    metadata = _model_or_mapping(trace.get("metadata"))
    citations = metadata.get("citations")
    if isinstance(citations, dict):
        for citation in citations.values():
            if not isinstance(citation, dict):
                continue
            source = citation.get("source")
            source = source if isinstance(source, dict) else {}
            for key in ("document_id", "asset_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    touched.add(value)
    for message in trace.get("context") or []:
        payload = _model_or_mapping(message)
        eval_message = _model_or_mapping(payload.get("eval_message"))
        source = _model_or_mapping(eval_message.get("source"))
        for key in ("document_id", "asset_id"):
            value = str(source.get(key) or "").strip()
            if value:
                touched.add(value)
    return touched


def _failure_buckets(
    *,
    status: str,
    workflow_result: dict[str, Any],
    tool_call_results: dict[str, Any],
    argument_result: dict[str, Any],
    target_result: dict[str, Any],
    evidence_results: dict[str, Any],
    inspect_funnel_result: dict[str, Any],
    citation_results: dict[str, Any],
    answer_result: dict[str, Any],
) -> list[str]:
    buckets: list[str] = []
    if status != "completed":
        buckets.append("run_failed")
    if (
        workflow_result.get("required_tool_recall") != 1.0
        or workflow_result.get("tool_order_score") != 1.0
    ):
        buckets.append("wrong_workflow")
    if _workflow_phase_failed(workflow_result.get("pre_search")):
        buckets.append("wrong_pre_search_workflow")
    if _workflow_phase_failed(workflow_result.get("post_search")):
        buckets.append("wrong_post_search_workflow")
    if tool_call_results.get("modality_route_accuracy") != 1.0:
        buckets.append("wrong_modality")
    for unmatched_target in target_result.get("unmatched_targets") or []:
        if not isinstance(unmatched_target, dict):
            continue
        hard_failures = unmatched_target.get("hard_failures") or []
        if not hard_failures:
            _append_unique(buckets, "missing_gold_tool_step")
            continue
        for bucket in _argument_failure_buckets(hard_failures):
            _append_unique(buckets, bucket)
    for unmatched_call in argument_result.get("unmatched_expected_calls") or []:
        if not isinstance(unmatched_call, dict):
            continue
        if unmatched_call.get("reason") == "missing_tool_step":
            _append_unique(buckets, "missing_gold_tool_step")
            continue
    inspect_items = [
        item for item in inspect_funnel_result.get("items") or [] if isinstance(item, dict)
    ]
    stable_inspect_items = (
        inspect_items if inspect_funnel_result.get("uses_stable_projection") else []
    )
    if stable_inspect_items:
        if any(not item.get("candidate_exposed") for item in stable_inspect_items):
            buckets.append("retrieval_miss")
        for item in stable_inspect_items:
            if item.get("candidate_exposed") and not item.get("inspect_targeted"):
                _append_unique(buckets, "gold_exposed_but_not_inspected")
            if (
                item.get("candidate_exposed")
                and item.get("inspect_targeted")
                and not item.get("inspect_return_aligned")
            ):
                _append_unique(buckets, "inspect_miss")
    elif evidence_results.get("gold_citation_count"):
        if evidence_results.get("candidate_exposure_recall") != 1.0:
            buckets.append("retrieval_miss")
        for item in evidence_results.get("funnel") or []:
            if not isinstance(item, dict):
                continue
            if item.get("candidate_exposed") and not item.get("inspected"):
                _append_unique(buckets, "gold_exposed_but_not_inspected")
            if (
                item.get("candidate_exposed")
                and item.get("inspected")
                and not item.get("inspect_locator_hit")
            ):
                _append_unique(buckets, "inspect_miss")
    if citation_results.get("citation_format_error"):
        buckets.append("citation_error")
    stable_inspect_complete = bool(stable_inspect_items) and all(
        item.get("inspect_return_aligned") for item in stable_inspect_items
    )
    stable_citation_complete = bool(stable_inspect_items) and all(
        item.get("inspect_chain_completed") for item in stable_inspect_items
    )
    legacy_citation_drop = (
        not stable_inspect_items
        and evidence_results.get("inspect_gold_recall") == 1.0
        and citation_results.get("agent_gold_usage_recall") != 1.0
    )
    if (stable_inspect_complete and not stable_citation_complete) or legacy_citation_drop:
        _append_unique(buckets, "agent_missed_citation")
    if answer_result.get("answer_empty"):
        buckets.append("answer_error")
    return buckets


def _workflow_phase_failed(value: Any) -> bool:
    payload = _model_or_mapping(value)
    if not payload or payload.get("required_tool_count") == 0:
        return False
    return payload.get("required_tool_recall") != 1.0 or payload.get("tool_order_score") != 1.0


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _model_or_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
        return payload if isinstance(payload, dict) else {}
    return {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 6)


def _avg(values: Any) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, int | float)]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 6)
