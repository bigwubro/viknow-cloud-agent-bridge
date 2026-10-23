"""Project durable runtime facts into a versioned learning sample.

This module deliberately has no persistence dependency.  Storage, queueing and
feedback revision are separate concerns; this boundary only defines the
recomputable semantic record they consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from viknow.core.eval.run_signals import RunSignal, project_run_signals
from viknow.core.runtime.domain import AgentEvent

LearningPrimaryLabel = Literal[
    "quality_failure",
    "capability_gap",
    "protection_success",
    "neutral",
]
LearningAttribution = Literal[
    "skill",
    "harness",
    "tool_or_index",
    "knowledge_source",
    "uncertain",
]
UserFeedback = Literal["helpful", "unhelpful"]
RunKind = Literal["production", "evaluation"]
FeedbackDisposition = Literal["helpful", "unhelpful", "withdrawn"]
LearningEvaluationVerdict = Literal["pass", "fail", "conditional_pass", "insufficient_evidence"]

_HARD_QUALITY_TAGS = {
    "final_answer_without_citation": "invalid_citation",
    "final_answer_missing": "insufficient_not_disclosed",
}


class LearningSignal(BaseModel):
    """A content-free fact retained by a learning sample."""

    kind: str
    severity: Literal["info", "warning"]
    event_sequences: list[int]
    attributes: dict[str, int | str | bool] = Field(default_factory=dict)

    @classmethod
    def from_run_signal(cls, signal: RunSignal) -> LearningSignal:
        return cls.model_validate(signal.model_dump())


class LearningFeedback(BaseModel):
    """The latest user-feedback revision used by one projection."""

    revision: int = Field(ge=1)
    disposition: FeedbackDisposition
    prior_disposition: UserFeedback | None = None


class LearningEvaluationConclusion(BaseModel):
    """Content-free evaluation conclusion that may be fused into a projection."""

    run_id: str
    verdict: LearningEvaluationVerdict
    evaluator_version: str
    reasons: list[str] = Field(default_factory=list, exclude=True)


class LearningProjectionIdentity(BaseModel):
    """Stable uniqueness fields for a future learning-store implementation."""

    run_id: str
    projection_version: int
    fusion_rule_version: str
    feedback_revision: int


class LearningSample(BaseModel):
    """Versioned interpretation of one production run without raw content."""

    schema_version: Literal[1] = 1
    projection_version: int = 1
    fusion_rule_version: str = "v1"
    run_id: str
    run_kind: RunKind = "production"
    eligible_for_learning: bool = True
    projection_identity: LearningProjectionIdentity
    primary_label: LearningPrimaryLabel
    issue_tags: list[str] = Field(default_factory=list)
    attribution: LearningAttribution
    confidence: Literal["high", "medium", "low"]
    feedback: LearningFeedback | None = None
    signals: list[LearningSignal] = Field(default_factory=list)


class LearningProjectionRecord(BaseModel):
    """Storage-ready projection envelope with an explicit idempotency key.

    This is a semantic contract only.  It deliberately does not depend on a
    database client or write a production learning record.
    """

    storage_key: str
    source_event_sequences: list[int] = Field(default_factory=list)
    sample: LearningSample

    @classmethod
    def from_sample(cls, sample: LearningSample) -> LearningProjectionRecord:
        identity = sample.projection_identity
        source_event_sequences = sorted(
            {sequence for signal in sample.signals for sequence in signal.event_sequences}
        )
        return cls(
            storage_key=(
                f"{identity.run_id}:v{identity.projection_version}:"
                f"{identity.fusion_rule_version}:feedback-{identity.feedback_revision}"
            ),
            source_event_sequences=source_event_sequences,
            sample=sample,
        )


def project_learning_sample(
    events: Iterable[AgentEvent],
    *,
    user_feedback: UserFeedback | None = None,
    feedback: LearningFeedback | None = None,
    run_kind: RunKind = "production",
    evaluation: LearningEvaluationConclusion | None = None,
) -> LearningSample:
    """Build the deterministic first projection for one completed run.

    Feedback is retained as an independent signal.  It cannot erase a hard
    runtime fact or manufacture an attribution on its own.
    """

    if user_feedback is not None and feedback is not None:
        raise ValueError("user_feedback and feedback cannot both be provided")

    projection = project_run_signals(events)
    if evaluation is not None and evaluation.run_id != projection.run_id:
        raise ValueError("evaluation conclusion run_id must match projected events")
    signals = [LearningSignal.from_run_signal(item) for item in projection.signals]
    if feedback is None and user_feedback is not None:
        feedback = LearningFeedback(revision=1, disposition=user_feedback)
    if feedback is not None:
        signal_kind = (
            f"user_{feedback.disposition}"
            if feedback.disposition != "withdrawn"
            else "user_feedback_withdrawn"
        )
        signals.append(
            LearningSignal(
                kind=signal_kind,
                severity="info",
                event_sequences=[],
            )
        )
    if evaluation is not None:
        signals.append(
            LearningSignal(
                kind=f"skill_flow_verdict_{evaluation.verdict}",
                severity="warning" if evaluation.verdict == "fail" else "info",
                event_sequences=[],
                attributes={"evaluator_version": evaluation.evaluator_version},
            )
        )

    kinds = {signal.kind for signal in signals}
    issue_tags = [tag for kind, tag in _HARD_QUALITY_TAGS.items() if kind in kinds]
    if feedback is not None and feedback.disposition != "withdrawn":
        issue_tags.append(f"user_{feedback.disposition}")

    if issue_tags and any(tag in _HARD_QUALITY_TAGS.values() for tag in issue_tags):
        primary_label: LearningPrimaryLabel = "quality_failure"
        attribution: LearningAttribution = "uncertain"
        confidence: Literal["high", "medium", "low"] = "high"
    elif evaluation is not None and evaluation.verdict == "fail":
        primary_label = "quality_failure"
        attribution = "uncertain"
        confidence = "medium"
        issue_tags = ["eval_quality_failure", *issue_tags]
    elif "stop_gate_recovered" in kinds:
        primary_label: LearningPrimaryLabel = "protection_success"
        attribution: LearningAttribution = "harness"
        confidence: Literal["high", "medium", "low"] = "high"
        issue_tags = ["step_blocked", "guard_recovery_success", *issue_tags]
    else:
        primary_label = "neutral"
        attribution = "uncertain"
        confidence = "low"

    return LearningSample(
        run_id=projection.run_id,
        run_kind=run_kind,
        eligible_for_learning=run_kind == "production",
        projection_identity=LearningProjectionIdentity(
            run_id=projection.run_id,
            projection_version=1,
            fusion_rule_version="v1",
            feedback_revision=feedback.revision if feedback else 0,
        ),
        primary_label=primary_label,
        issue_tags=issue_tags,
        attribution=attribution,
        confidence=confidence,
        feedback=feedback,
        signals=signals,
    )


def project_learning_record(
    events: Iterable[AgentEvent],
    *,
    user_feedback: UserFeedback | None = None,
    feedback: LearningFeedback | None = None,
    run_kind: RunKind = "production",
    evaluation: LearningEvaluationConclusion | None = None,
) -> LearningProjectionRecord:
    """Build one storage-ready projection record without persisting it."""

    return LearningProjectionRecord.from_sample(
        project_learning_sample(
            events,
            user_feedback=user_feedback,
            feedback=feedback,
            run_kind=run_kind,
            evaluation=evaluation,
        )
    )
