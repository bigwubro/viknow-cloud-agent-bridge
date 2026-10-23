"""Deterministic repeated-run stability summaries for offline quality scorecards."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from math import sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.quality_score import QualityScorecard


class RepeatedQualityRun(BaseModel):
    """The bounded score-bearing facts from one repeat of one case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    status: str
    quality_scorecard: QualityScorecard


class CaseStabilitySummary(BaseModel):
    """Observed stability facts only; it intentionally does not decide acceptance."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    run_count: int = Field(ge=1)
    status_counts: dict[str, int]
    optimization_score_mean: float | None
    optimization_score_stddev: float | None
    optimization_score_range: float | None


class SkillFlowStabilityReport(BaseModel):
    """Provisional repeat summary for one uniform quality scorer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skill-flow-stability.v1"] = "viknow.skill-flow-stability.v1"
    evaluation_stage: Literal["provisional"] = "provisional"
    release_eligible: Literal[False] = False
    scorer_version: str
    cases: list[CaseStabilitySummary]


def summarize_repeated_quality_runs(
    runs: Iterable[RepeatedQualityRun],
) -> SkillFlowStabilityReport:
    """Summarize repeated results without turning score variance into a release gate."""

    items = list(runs)
    if not items:
        raise ValueError("at least one repeated quality run is required")
    versions = {item.quality_scorecard.scorer_version for item in items}
    if len(versions) != 1:
        raise ValueError("repeated quality run scorer version differs")
    grouped: dict[str, list[RepeatedQualityRun]] = defaultdict(list)
    for item in items:
        grouped[item.case_id].append(item)
    return SkillFlowStabilityReport(
        scorer_version=next(iter(versions)),
        cases=[_summarize_case(case_id, grouped[case_id]) for case_id in sorted(grouped)],
    )


def _summarize_case(case_id: str, runs: list[RepeatedQualityRun]) -> CaseStabilitySummary:
    scores = [run.quality_scorecard.optimization_score for run in runs]
    defined_scores = [score for score in scores if score is not None]
    mean = round(sum(defined_scores) / len(defined_scores), 4) if defined_scores else None
    if mean is None:
        stddev = None
        score_range = None
    else:
        stddev = round(
            sqrt(sum((score - mean) ** 2 for score in defined_scores) / len(defined_scores)),
            4,
        )
        score_range = round(max(defined_scores) - min(defined_scores), 4)
    return CaseStabilitySummary(
        case_id=case_id,
        run_count=len(runs),
        status_counts=dict(sorted(Counter(run.status for run in runs).items())),
        optimization_score_mean=mean,
        optimization_score_stddev=stddev,
        optimization_score_range=score_range,
    )
