"""Content-free observation summaries over deterministic learning candidates."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.learning.candidates import LearningCandidate, LearningCandidateBucket


class LearningCandidateCluster(BaseModel):
    """A deterministic signal cluster, not a semantic topic or causal conclusion."""

    model_config = ConfigDict(extra="forbid")

    signature: str
    bucket: LearningCandidateBucket
    reason_codes: list[str]
    candidate_count: int = Field(ge=1)
    high_priority_count: int = Field(ge=0)
    run_ids: list[str]


class LearningObservationReport(BaseModel):
    """A non-release summary suitable for later human review and calibration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.learning-observations.v1"] = "viknow.learning-observations.v1"
    release_eligible: Literal[False] = False
    total_candidate_count: int = Field(ge=0)
    bucket_counts: dict[LearningCandidateBucket, int]
    clusters: list[LearningCandidateCluster]


def summarize_learning_candidates(
    candidates: Iterable[LearningCandidate],
) -> LearningObservationReport:
    """Group candidates by existing deterministic signal reasons only."""

    items = list(candidates)
    grouped: dict[tuple[LearningCandidateBucket, tuple[str, ...]], list[LearningCandidate]] = (
        defaultdict(list)
    )
    for candidate in items:
        reasons = tuple(sorted(set(candidate.reason_codes)))
        grouped[(candidate.bucket, reasons)].append(candidate)
    clusters = [
        _cluster(bucket, reasons, records) for (bucket, reasons), records in grouped.items()
    ]
    clusters.sort(key=lambda item: (-item.candidate_count, item.signature))
    counts = Counter(candidate.bucket for candidate in items)
    return LearningObservationReport(
        total_candidate_count=len(items),
        bucket_counts={bucket: counts[bucket] for bucket in ("bad", "good", "uncertain")},
        clusters=clusters,
    )


def _cluster(
    bucket: LearningCandidateBucket,
    reason_codes: tuple[str, ...],
    candidates: list[LearningCandidate],
) -> LearningCandidateCluster:
    reason_key = ",".join(reason_codes) if reason_codes else "no_reason_code"
    return LearningCandidateCluster(
        signature=f"{bucket}:{reason_key}",
        bucket=bucket,
        reason_codes=list(reason_codes),
        candidate_count=len(candidates),
        high_priority_count=sum(candidate.priority == "high" for candidate in candidates),
        run_ids=sorted(candidate.run_id for candidate in candidates),
    )
