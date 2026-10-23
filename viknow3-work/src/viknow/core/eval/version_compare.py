"""Same-condition comparison for offline Skill Flow quality scorecards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.eval.quality_score import QualityScorecard


class QualityScoreDelta(BaseModel):
    """One case's immutable scorecard delta between two evaluated versions."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    baseline_optimization_score: float | None
    candidate_optimization_score: float | None
    optimization_score_delta: float | None
    dimension_deltas: dict[str, float | None]


class ExecutionMetricSummary(BaseModel):
    """Observed execution aggregate; absent metrics remain absent rather than zero."""

    model_config = ConfigDict(extra="forbid")

    observed_case_count: int
    total: float | None
    average: float | None


class ExecutionMetricDelta(BaseModel):
    """One aggregate metric comparison with explicit observation coverage."""

    model_config = ConfigDict(extra="forbid")

    baseline: ExecutionMetricSummary
    candidate: ExecutionMetricSummary
    total_delta: float | None


class ExecutionRunSummary(BaseModel):
    """Terminal run outcome counts for an evaluated version."""

    model_config = ConfigDict(extra="forbid")

    non_completed_case_count: int


class ExecutionComparison(BaseModel):
    """Non-quality execution facts for a same-case version comparison."""

    model_config = ConfigDict(extra="forbid")

    baseline: ExecutionRunSummary
    candidate: ExecutionRunSummary
    non_completed_case_delta: int
    latency_seconds: ExecutionMetricDelta
    token_input: ExecutionMetricDelta
    token_output: ExecutionMetricDelta
    tool_call_count: ExecutionMetricDelta


class AcceptedComparisonProvenance(BaseModel):
    """Proof that both accepted-stage runs loaded the compared Skill bytes."""

    model_config = ConfigDict(extra="forbid")

    oracle_storage_key: str = Field(min_length=1)
    baseline_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_snapshot_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_snapshot_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)
    baseline_trace_id: str = Field(min_length=1)
    candidate_trace_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distinct_candidate(self) -> AcceptedComparisonProvenance:
        if self.baseline_skill_sha256 == self.candidate_skill_sha256:
            raise ValueError("accepted comparison requires a distinct candidate Skill")
        return self


class SkillFlowVersionComparison(BaseModel):
    """Same-condition comparison that deliberately makes no release decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skill-flow-version-comparison.v1"] = (
        "viknow.skill-flow-version-comparison.v1"
    )
    evaluation_stage: Literal["provisional", "accepted"] = "provisional"
    release_eligible: Literal[False] = False
    scorer_version: str
    case_count: int
    baseline_optimization_score: float | None
    candidate_optimization_score: float | None
    optimization_score_delta: float | None
    cases: list[QualityScoreDelta]
    execution: ExecutionComparison | None = None
    accepted_provenance: AcceptedComparisonProvenance | None = None

    @model_validator(mode="after")
    def validate_stage_provenance(self) -> SkillFlowVersionComparison:
        if (self.evaluation_stage == "accepted") != (self.accepted_provenance is not None):
            raise ValueError("accepted comparison stage requires accepted provenance")
        return self


def compare_quality_scorecards(
    *,
    baseline: Mapping[str, QualityScorecard],
    candidate: Mapping[str, QualityScorecard],
) -> SkillFlowVersionComparison:
    """Compare exactly the same cases under one scorecard version.

    The artifact is intentionally provisional: human-reviewed Oracle/holdout
    requirements remain outside this deterministic comparison.
    """

    baseline_ids = set(baseline)
    candidate_ids = set(candidate)
    if baseline_ids != candidate_ids:
        raise ValueError("baseline and candidate case sets differ")
    scorer_versions = {
        scorecard.scorer_version for scorecard in (*baseline.values(), *candidate.values())
    }
    if len(scorer_versions) != 1:
        raise ValueError("baseline and candidate scorer version differs")
    scorer_version = next(iter(scorer_versions), "")
    cases = [
        _case_delta(case_id, baseline[case_id], candidate[case_id])
        for case_id in sorted(baseline_ids)
    ]
    baseline_scores = [baseline[case_id].optimization_score for case_id in sorted(baseline_ids)]
    candidate_scores = [candidate[case_id].optimization_score for case_id in sorted(candidate_ids)]
    baseline_average = _average(baseline_scores)
    candidate_average = _average(candidate_scores)
    return SkillFlowVersionComparison(
        scorer_version=scorer_version,
        case_count=len(cases),
        baseline_optimization_score=baseline_average,
        candidate_optimization_score=candidate_average,
        optimization_score_delta=_delta(baseline_average, candidate_average),
        cases=cases,
    )


def compare_quality_run_payloads(
    *,
    baseline: list[Mapping[str, Any]],
    candidate: list[Mapping[str, Any]],
) -> SkillFlowVersionComparison:
    """Compare scorecards and the execution facts already present in result bundles."""

    baseline_by_case = _runs_by_case(baseline, label="baseline")
    candidate_by_case = _runs_by_case(candidate, label="candidate")
    comparison = compare_quality_scorecards(
        baseline={case_id: _quality_scorecard(run) for case_id, run in baseline_by_case.items()},
        candidate={case_id: _quality_scorecard(run) for case_id, run in candidate_by_case.items()},
    )
    baseline_runs = [baseline_by_case[case_id] for case_id in sorted(baseline_by_case)]
    candidate_runs = [candidate_by_case[case_id] for case_id in sorted(candidate_by_case)]
    return comparison.model_copy(
        update={"execution": _execution_comparison(baseline_runs, candidate_runs)}
    )


def compare_accepted_quality_run_payloads(
    *,
    baseline: list[Mapping[str, Any]],
    candidate: list[Mapping[str, Any]],
    provenance: AcceptedComparisonProvenance,
) -> SkillFlowVersionComparison:
    """Compare accepted-stage runs after validating their behavior-snapshot proof."""

    if provenance.baseline_snapshot_skill_sha256 != provenance.baseline_skill_sha256:
        raise ValueError("baseline snapshot Skill hash mismatch")
    if provenance.candidate_snapshot_skill_sha256 != provenance.candidate_skill_sha256:
        raise ValueError("candidate snapshot Skill hash mismatch")
    comparison = compare_quality_run_payloads(baseline=baseline, candidate=candidate)
    return comparison.model_copy(
        update={
            "evaluation_stage": "accepted",
            "accepted_provenance": provenance,
        }
    )


def _case_delta(
    case_id: str,
    baseline: QualityScorecard,
    candidate: QualityScorecard,
) -> QualityScoreDelta:
    dimensions = (
        "answer_evidence",
        "process_compliance",
        "efficiency_convergence",
        "release_safety",
    )
    return QualityScoreDelta(
        case_id=case_id,
        baseline_optimization_score=baseline.optimization_score,
        candidate_optimization_score=candidate.optimization_score,
        optimization_score_delta=_delta(
            baseline.optimization_score,
            candidate.optimization_score,
        ),
        dimension_deltas={
            dimension: _delta(
                getattr(baseline, dimension).score,
                getattr(candidate, dimension).score,
            )
            for dimension in dimensions
        },
    )


def _runs_by_case(
    runs: list[Mapping[str, Any]],
    *,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        case_id = str(run.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"{label} run is missing case_id")
        if case_id in result:
            raise ValueError(f"duplicate {label} run case: {case_id}")
        result[case_id] = run
    return result


def _quality_scorecard(run: Mapping[str, Any]) -> QualityScorecard:
    raw_scorecard = run.get("quality_scorecard")
    if not isinstance(raw_scorecard, Mapping):
        raise ValueError("version comparison requires quality-scored runs")
    return QualityScorecard.model_validate(raw_scorecard)


def _execution_comparison(
    baseline: list[Mapping[str, Any]],
    candidate: list[Mapping[str, Any]],
) -> ExecutionComparison:
    baseline_non_completed = _non_completed_summary(baseline)
    candidate_non_completed = _non_completed_summary(candidate)
    return ExecutionComparison(
        baseline=baseline_non_completed,
        candidate=candidate_non_completed,
        non_completed_case_delta=(
            candidate_non_completed.non_completed_case_count
            - baseline_non_completed.non_completed_case_count
        ),
        latency_seconds=_execution_metric_delta(baseline, candidate, "latency_seconds"),
        token_input=_execution_metric_delta(baseline, candidate, "token_input"),
        token_output=_execution_metric_delta(baseline, candidate, "token_output"),
        tool_call_count=_execution_metric_delta(baseline, candidate, "tool_call_count"),
    )


def _non_completed_summary(runs: list[Mapping[str, Any]]) -> ExecutionRunSummary:
    count = sum(str(run.get("status") or "") != "completed" for run in runs)
    return ExecutionRunSummary(non_completed_case_count=count)


def _execution_metric_delta(
    baseline: list[Mapping[str, Any]],
    candidate: list[Mapping[str, Any]],
    metric: str,
) -> ExecutionMetricDelta:
    baseline_summary = _execution_metric_summary(baseline, metric)
    candidate_summary = _execution_metric_summary(candidate, metric)
    return ExecutionMetricDelta(
        baseline=baseline_summary,
        candidate=candidate_summary,
        total_delta=_delta(baseline_summary.total, candidate_summary.total),
    )


def _execution_metric_summary(
    runs: list[Mapping[str, Any]],
    metric: str,
) -> ExecutionMetricSummary:
    values: list[float] = []
    for run in runs:
        efficiency = run.get("efficiency_result")
        if not isinstance(efficiency, Mapping):
            continue
        value = efficiency.get(metric)
        if isinstance(value, int | float):
            values.append(float(value))
    total = round(sum(values), 4) if values else None
    return ExecutionMetricSummary(
        observed_case_count=len(values),
        total=total,
        average=round(total / len(values), 4) if total is not None else None,
    )


def _average(values: list[float | None]) -> float | None:
    defined = [value for value in values if value is not None]
    if not defined:
        return None
    return round(sum(defined) / len(defined), 4)


def _delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return round(candidate - baseline, 4)
