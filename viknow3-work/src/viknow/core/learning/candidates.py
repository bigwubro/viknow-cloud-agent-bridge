"""Deterministic, content-free selection of learning samples for later review."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.learning.projection import LearningProjectionRecord

LearningCandidateBucket = Literal["good", "bad", "uncertain"]
LearningCandidateRisk = Literal["low", "medium", "high"]


class LearningCandidate(BaseModel):
    """A review candidate, not an Oracle fact or automatic optimization input."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    storage_key: str
    bucket: LearningCandidateBucket
    priority: Literal["high", "normal"]
    reason_codes: list[str] = Field(default_factory=list)
    source_event_sequences: list[int] = Field(default_factory=list)


def select_learning_candidates(
    records: Iterable[LearningProjectionRecord],
) -> list[LearningCandidate]:
    """Classify the latest eligible projection per run without inventing gold facts.

    ``good`` requires either explicit helpful feedback or a final passing Answer
    Review, together with no warning-level runtime signal. ``bad`` represents
    hard runtime quality facts, a final failing Answer Review, or explicit
    negative feedback. Everything else remains ``uncertain`` for later review.
    """

    latest_by_run: dict[str, LearningProjectionRecord] = {}
    for record in records:
        sample = record.sample
        if not sample.eligible_for_learning:
            continue
        existing = latest_by_run.get(sample.run_id)
        if existing is None or _revision(record) > _revision(existing):
            latest_by_run[sample.run_id] = record
    return [_candidate(record) for _, record in sorted(latest_by_run.items())]


def learning_candidate_risk(candidate: LearningCandidate) -> LearningCandidateRisk:
    """Map deterministic selection output to the shared review risk level."""

    if candidate.priority == "high":
        return "high"
    return "low" if candidate.bucket == "good" else "medium"


def _candidate(record: LearningProjectionRecord) -> LearningCandidate:
    sample = record.sample
    signal_kinds = {signal.kind for signal in sample.signals}
    hard = {"invalid_citation", "insufficient_not_disclosed"} & set(sample.issue_tags)
    feedback = sample.feedback.disposition if sample.feedback is not None else None
    warning_signals = [signal for signal in sample.signals if signal.severity == "warning"]
    if hard:
        return _build_candidate(record, "bad", "high", sorted(hard))
    if "answer_review_failed" in signal_kinds:
        return _build_candidate(record, "bad", "high", ["answer_review_failed"])
    if feedback == "unhelpful":
        return _build_candidate(record, "bad", "high", ["user_unhelpful"])
    if not warning_signals and (feedback == "helpful" or "answer_review_passed" in signal_kinds):
        positive_reason = "user_helpful" if feedback == "helpful" else "answer_review_passed"
        return _build_candidate(record, "good", "normal", [positive_reason, "runtime_clean"])
    reasons = sorted(signal_kinds) if signal_kinds else ["insufficient_signals"]
    return _build_candidate(record, "uncertain", "normal", reasons)


def _build_candidate(
    record: LearningProjectionRecord,
    bucket: LearningCandidateBucket,
    priority: Literal["high", "normal"],
    reason_codes: list[str],
) -> LearningCandidate:
    return LearningCandidate(
        run_id=record.sample.run_id,
        storage_key=record.storage_key,
        bucket=bucket,
        priority=priority,
        reason_codes=reason_codes,
        source_event_sequences=record.source_event_sequences,
    )


def _revision(record: LearningProjectionRecord) -> int:
    return record.sample.projection_identity.feedback_revision
