"""Deterministic, content-free sampling for later human label calibration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from viknow.core.learning.projection import (
    LearningPrimaryLabel,
    LearningProjectionRecord,
)


class CalibrationCandidate(BaseModel):
    """A pending human-review item; it never records a reviewer conclusion."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    storage_key: str
    primary_label: LearningPrimaryLabel
    confidence: Literal["high", "medium", "low"]
    selection_reasons: list[str]
    source_event_sequences: list[int]
    review_status: Literal["pending"] = "pending"


def select_calibration_candidates(
    records: Iterable[LearningProjectionRecord],
    *,
    limit: int,
) -> list[CalibrationCandidate]:
    """Select the latest eligible projections with deterministic risk stratification."""

    if limit < 1:
        raise ValueError("calibration sample limit must be positive")
    latest_by_run: dict[str, LearningProjectionRecord] = {}
    for record in records:
        sample = record.sample
        if not sample.eligible_for_learning:
            continue
        existing = latest_by_run.get(sample.run_id)
        if existing is None or _feedback_revision(record) > _feedback_revision(existing):
            latest_by_run[sample.run_id] = record
    ranked = sorted(latest_by_run.values(), key=_rank)
    selected: list[LearningProjectionRecord] = []
    selected_keys: set[str] = set()
    covered_labels: set[LearningPrimaryLabel] = set()
    for item in ranked:
        if len(selected) >= limit:
            break
        label = item.sample.primary_label
        if label not in covered_labels:
            selected.append(item)
            selected_keys.add(item.storage_key)
            covered_labels.add(label)
    for item in ranked:
        if len(selected) >= limit:
            break
        if item.storage_key not in selected_keys:
            selected.append(item)
    return [_candidate(record) for record in selected]


def _candidate(record: LearningProjectionRecord) -> CalibrationCandidate:
    sample = record.sample
    return CalibrationCandidate(
        run_id=sample.run_id,
        storage_key=record.storage_key,
        primary_label=sample.primary_label,
        confidence=sample.confidence,
        selection_reasons=_selection_reasons(record),
        source_event_sequences=record.source_event_sequences,
    )


def _rank(record: LearningProjectionRecord) -> tuple[int, str]:
    reasons = _selection_reasons(record)
    priority = 0 if "feedback_conflicts_with_hard_quality_fact" in reasons else 2
    if "low_confidence" in reasons:
        priority = 1
    return priority, record.sample.run_id


def _selection_reasons(record: LearningProjectionRecord) -> list[str]:
    sample = record.sample
    hard_quality_tags = {"invalid_citation", "insufficient_not_disclosed"}
    if (
        sample.feedback
        and sample.feedback.disposition == "helpful"
        and hard_quality_tags.intersection(sample.issue_tags)
    ):
        return ["feedback_conflicts_with_hard_quality_fact"]
    if sample.confidence == "low":
        return ["low_confidence"]
    return [f"label_stratum:{sample.primary_label}"]


def _feedback_revision(record: LearningProjectionRecord) -> int:
    return record.sample.projection_identity.feedback_revision
