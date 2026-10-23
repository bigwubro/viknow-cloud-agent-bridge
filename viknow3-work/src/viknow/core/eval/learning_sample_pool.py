"""Offline, review-gated learning samples for Skill Flow calibration.

The pool deliberately sits outside the production Learning Projection outbox:
evaluation content, candidate evidence, and a human Gate-B conclusion need a
different retention boundary.  A candidate package is never an accepted Oracle
package merely because it was collected successfully.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.eval.candidate_evidence import (
    AcceptedOraclePackage,
    CandidateEvidencePackage,
    GateBDecision,
    GateBStatus,
    build_accepted_oracle_package,
)
from viknow.core.learning.candidates import LearningCandidateBucket

LearningScenario = Literal[
    "online_no_annotation_no_answer",
    "dataset_no_annotation_with_answer",
    "dataset_with_annotation_with_answer",
]


class LearningSampleRunFacts(BaseModel):
    """Bounded execution facts retained with a review sample."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    trace_id: str | None = None
    status: str = Field(min_length=1)
    tool_call_count: int = Field(ge=0)
    run_signals: list[dict[str, Any]] = Field(default_factory=list)
    answer_review: dict[str, Any] | None = None
    stop_gate: dict[str, Any] | None = None


class GateBReviewConclusion(BaseModel):
    """Human Gate-B conclusion; pending/rejected conclusions adopt nothing."""

    model_config = ConfigDict(extra="forbid")

    status: GateBStatus = "pending"
    decision_ref: str | None = None
    accepted_fact_ids: list[str] = Field(default_factory=list)
    note: str | None = None

    @classmethod
    def from_decision(cls, decision: GateBDecision) -> GateBReviewConclusion:
        return cls(
            status=decision.status,
            decision_ref=decision.decision_ref,
            accepted_fact_ids=decision.accepted_fact_ids,
        )

    @model_validator(mode="after")
    def validate_explicit_acceptance(self) -> GateBReviewConclusion:
        accepted = self.status in {"accepted", "revised"}
        if accepted and (not self.decision_ref or not self.accepted_fact_ids):
            raise ValueError("accepted Gate B conclusion requires decision_ref and fact ids")
        if not accepted and self.accepted_fact_ids:
            raise ValueError("non-accepted Gate B conclusion cannot select fact ids")
        return self


class LearningSamplePoolRecord(BaseModel):
    """One human-reviewable sample, never an automatic optimizer input."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.learning-sample-pool.v1"] = "viknow.learning-sample-pool.v1"
    case_id: str = Field(min_length=1)
    scenario: LearningScenario
    bucket: LearningCandidateBucket
    reason_codes: list[str] = Field(default_factory=list)
    run_facts: LearningSampleRunFacts
    collection_run_facts: LearningSampleRunFacts | None = None
    candidate_evidence: CandidateEvidencePackage | None = None
    gate_b: GateBReviewConclusion = Field(default_factory=GateBReviewConclusion)
    accepted_oracle: AcceptedOraclePackage | None = None

    @property
    def auto_adopted(self) -> bool:
        """Always false: optimizer admission is a separate explicit operation."""

        return False

    @property
    def eligible_for_scoring(self) -> bool:
        return self.accepted_oracle is not None and self.gate_b.status in {"accepted", "revised"}

    @model_validator(mode="after")
    def validate_evidence_review_chain(self) -> LearningSamplePoolRecord:
        if self.candidate_evidence is None:
            if self.accepted_oracle is not None:
                raise ValueError("accepted_oracle requires candidate_evidence")
            if self.gate_b.status in {"accepted", "revised"}:
                raise ValueError("accepted Gate B conclusion requires candidate_evidence")
            return self

        task = self.candidate_evidence.task
        if task.case_id != self.case_id:
            raise ValueError("candidate_evidence case_id must match sample case_id")
        if self.run_facts.run_id not in task.source_run_ids:
            raise ValueError("candidate_evidence must reference sample run_id")
        accepted = self.gate_b.status in {"accepted", "revised"}
        if accepted != (self.accepted_oracle is not None):
            raise ValueError("accepted_oracle is required exactly for accepted Gate B conclusions")
        if self.accepted_oracle is None:
            return self
        if (
            self.accepted_oracle.human_revision is None
            and (task_research_outcome := self.candidate_evidence.research_outcome) is not None
            and task_research_outcome.status == "research_incomplete"
        ):
            raise ValueError("research_incomplete candidate cannot be eligible for scoring")
        rebuilt = build_accepted_oracle_package(
            candidate=self.candidate_evidence,
            decision=self.accepted_oracle.decision,
            human_revision=self.accepted_oracle.human_revision,
        )
        if rebuilt != self.accepted_oracle:
            raise ValueError("accepted Oracle must match its source and review decision")
        if self.accepted_oracle.case_id != self.case_id:
            raise ValueError("accepted_oracle case_id must match sample case_id")
        if self.accepted_oracle.task_id != task.task_id:
            raise ValueError("accepted_oracle task_id must match candidate task")
        if self.accepted_oracle.decision.decision_ref != self.gate_b.decision_ref:
            raise ValueError("accepted_oracle decision must match Gate B conclusion")
        if self.accepted_oracle.decision.status != self.gate_b.status:
            raise ValueError("accepted_oracle status must match Gate B conclusion")
        if self.accepted_oracle.decision.accepted_fact_ids != self.gate_b.accepted_fact_ids:
            raise ValueError("accepted_oracle fact ids must match Gate B conclusion")
        return self


class _LearningSamplePoolDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.learning-sample-pool-document.v1"] = (
        "viknow.learning-sample-pool-document.v1"
    )
    records: list[LearningSamplePoolRecord]


def write_learning_sample_pool(path: Path, records: list[LearningSamplePoolRecord]) -> None:
    """Persist review records deterministically; writing does not adopt any fact."""

    document = _LearningSamplePoolDocument(records=records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def load_learning_sample_pool(path: Path) -> list[LearningSamplePoolRecord]:
    """Load a persisted pool without making a scoring or adoption decision."""

    return _LearningSamplePoolDocument.model_validate_json(path.read_bytes()).records


def learning_sample_from_eval_trace(
    *,
    case_id: str,
    scenario: LearningScenario,
    trace: Mapping[str, Any],
    source_trace: Mapping[str, Any] | None = None,
) -> LearningSamplePoolRecord:
    """Project one normal or Oracle trace into a non-adopted review sample.

    An Oracle collection run points back to the QA source run through its
    Gate-A task.  In that case both run facts are retained: ``run_facts`` is
    the source QA run and ``collection_run_facts`` records collection quality.
    """

    collection_facts = _run_facts_from_trace(trace)
    candidate = _candidate_evidence_from_trace(trace)
    if candidate is None:
        bucket, reasons = _bucket_for_run(collection_facts)
        return LearningSamplePoolRecord(
            case_id=case_id,
            scenario=scenario,
            bucket=bucket,
            reason_codes=reasons,
            run_facts=collection_facts,
        )

    source_facts = _run_facts_from_trace(source_trace) if source_trace is not None else None
    if source_facts is None or source_facts.run_id not in candidate.task.source_run_ids:
        raise ValueError("Oracle candidate requires its referenced source run trace")
    reason_codes = ["pending_gate_b_review"]
    if candidate.research_outcome is not None:
        reason_codes.append(candidate.research_outcome.status)
    return LearningSamplePoolRecord(
        case_id=case_id,
        scenario=scenario,
        bucket="uncertain",
        reason_codes=reason_codes,
        run_facts=source_facts,
        collection_run_facts=collection_facts,
        candidate_evidence=candidate,
    )


def _run_facts_from_trace(trace: Mapping[str, Any] | None) -> LearningSampleRunFacts:
    if trace is None:
        raise ValueError("trace is required")
    run_id = str(trace.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("trace run_id is required")
    metadata = trace.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    performance = trace.get("performance")
    performance = performance if isinstance(performance, Mapping) else {}
    return LearningSampleRunFacts(
        run_id=run_id,
        trace_id=_optional_text(trace.get("langfuse_trace_id")),
        status=str(trace.get("status") or "unknown"),
        tool_call_count=_nonnegative_int(performance.get("tool_call_count")),
        run_signals=_dict_list(metadata.get("run_signals")),
        answer_review=_mapping_or_none(metadata.get("answer_review")),
        stop_gate=_mapping_or_none(metadata.get("stop_gate")),
    )


def _candidate_evidence_from_trace(trace: Mapping[str, Any]) -> CandidateEvidencePackage | None:
    metadata = trace.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    packages = metadata.get("candidate_evidence_packages")
    if not isinstance(packages, list) or not packages:
        return None
    if len(packages) != 1 or not isinstance(packages[0], Mapping):
        raise ValueError("eval trace must contain at most one candidate evidence package")
    return CandidateEvidencePackage.model_validate(dict(packages[0]))


def _bucket_for_run(
    facts: LearningSampleRunFacts,
) -> tuple[LearningCandidateBucket, list[str]]:
    if facts.status != "completed":
        return "bad", [f"run_not_completed:{facts.status}"]
    warning = any(signal.get("severity") == "warning" for signal in facts.run_signals)
    if warning:
        return "uncertain", ["runtime_warning"]
    review = facts.answer_review or {}
    if review.get("passed") is True:
        return "good", ["answer_review_passed", "runtime_clean"]
    return "uncertain", ["insufficient_quality_signals"]


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _nonnegative_int(value: object) -> int:
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _mapping_or_none(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None
