"""Gate-bound candidate evidence contracts for later offline Oracle collection."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from viknow3.agents.runtimes.deepagents.core.eval.evidence_oracle import (
    AcceptedOracleFact,
    EvidenceDisposition,
    EvidenceOracleFact,
    EvidenceOraclePackage,
    EvidenceOracleRequirement,
    EvidenceOracleScope,
)
from viknow3.agents.runtimes.deepagents.core.eval.research_oracle import (
    EvidenceFrontierItem,
    ResearchOutcome,
    ResearchPlan,
    ResearchSeed,
    ResearchState,
    validate_frontier_scope,
    validate_research_seed_scope,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
GateAStatus = Literal["approved", "pending", "rejected"]
GateBStatus = Literal["pending", "accepted", "revised", "rejected", "needs_more_evidence"]


class EvidenceCollectionTask(BaseModel):
    """A bounded collection request created only after the human Gate A decision."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    material_revision: str = Field(min_length=1)
    source_run_ids: list[str] = Field(min_length=1)
    scope: EvidenceOracleScope
    gate_a_status: GateAStatus
    gate_a_decision_ref: str = Field(min_length=1)
    research_seeds: list[ResearchSeed] = Field(default_factory=list)
    research_plan: ResearchPlan | None = None
    research_frontier: list[EvidenceFrontierItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_research_seeds(self) -> EvidenceCollectionTask:
        validate_research_seed_scope(self.scope, self.research_seeds)
        validate_frontier_scope(self.scope, self.research_frontier)
        return self


class CandidateEvidenceFact(BaseModel):
    """One non-authoritative fact inferred by a scoped evidence collector."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fact_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    disposition: EvidenceDisposition
    statement: str = Field(min_length=1)
    evidence_refs: list[dict[str, Any]] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class CandidateEvidencePackage(BaseModel):
    """A scoped collection result that remains non-authoritative until Gate B."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow3.agents.runtime.candidate-evidence.v1"] = "viknow3.agents.runtime.candidate-evidence.v1"
    task: EvidenceCollectionTask
    facts: list[CandidateEvidenceFact]
    candidate_answer: str | None = None
    gate_b_status: GateBStatus = "pending"
    observed_asset_revisions: dict[str, str] = Field(default_factory=dict)
    research_state: ResearchState | None = None
    research_outcome: ResearchOutcome | None = None

    @model_validator(mode="after")
    def validate_research_terminal_consistency(self) -> CandidateEvidencePackage:
        if self.research_outcome is None:
            return self
        if self.research_outcome.status == "no_evidence_in_scope" and (
            self.facts or self.candidate_answer
        ):
            raise ValueError("no_evidence_in_scope cannot carry candidate facts or an answer")
        return self


class GateBDecision(BaseModel):
    """An explicit human decision selecting the candidate facts to adopt."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["accepted", "revised"]
    decision_ref: str = Field(min_length=1)
    accepted_fact_ids: list[str] = Field(min_length=1)


class AcceptedOracleRequirement(BaseModel):
    """A Gate-B-adopted requirement, preserving its candidate fact provenance."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    fact_ids: list[str] = Field(min_length=1)


class HumanEvidenceRevision(BaseModel):
    """Human-authored replacement facts, bound to the unchanged collection output."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision_id: str = Field(min_length=1)
    source_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_source_refs: list[NonEmptyText] = Field(min_length=1)
    facts: list[CandidateEvidenceFact] = Field(min_length=1)
    revised_answer: str | None = None


def candidate_package_sha256(candidate: CandidateEvidencePackage) -> str:
    serialized = json.dumps(
        candidate.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AcceptedOraclePackage(BaseModel):
    """Human-adopted evidence facts; this remains distinct from a scored result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow3.agents.runtime.accepted-oracle.v1"] = "viknow3.agents.runtime.accepted-oracle.v1"
    case_id: str
    task_id: str
    scope: EvidenceOracleScope
    decision: GateBDecision
    facts: list[AcceptedOracleFact] = Field(min_length=1)
    requirements: list[AcceptedOracleRequirement] = Field(min_length=1)
    human_revision: HumanEvidenceRevision | None = None
    reference_answer: str | None = None


def build_candidate_evidence_package(
    *,
    task: EvidenceCollectionTask,
    facts: list[CandidateEvidenceFact],
    candidate_answer: str | None,
    research_state: ResearchState | None = None,
    research_outcome: ResearchOutcome | None = None,
    observed_asset_revisions: dict[str, str] | None = None,
) -> CandidateEvidencePackage:
    """Build a non-authoritative package after validating Gate A and source scope."""

    if task.gate_a_status != "approved":
        raise ValueError("candidate evidence collection requires Gate A approval")
    normalized_facts = [
        fact.model_copy(
            update={
                "evidence_refs": [
                    _normalize_evidence_ref(evidence_ref) for evidence_ref in fact.evidence_refs
                ]
            }
        )
        for fact in facts
    ]
    if task.research_plan is not None:
        if research_state is None:
            raise ValueError("research state is required when task has a research plan")
        slot_ids = {item.id for item in task.research_plan.subquestions}
        invalid_ids = {fact.requirement_id for fact in normalized_facts} - slot_ids
        if invalid_ids:
            raise ValueError("candidate fact requirement_id must be a research plan slot")
        if research_outcome is not None:
            coverage_ids = [entry.requirement_id for entry in research_outcome.coverage]
            if set(coverage_ids) != slot_ids or len(coverage_ids) != len(set(coverage_ids)):
                raise ValueError("research outcome coverage ids must match ResearchPlan slot ids")
        if research_state is not None and research_state.plan != task.research_plan:
            raise ValueError("research state plan must match task research plan")
    for fact in normalized_facts:
        for evidence_ref in fact.evidence_refs:
            _validate_evidence_ref_scope(evidence_ref, task.scope)
    return CandidateEvidencePackage(
        task=task,
        facts=normalized_facts,
        candidate_answer=candidate_answer.strip() if candidate_answer else None,
        research_state=research_state,
        research_outcome=research_outcome,
        observed_asset_revisions=observed_asset_revisions or {},
    )


def build_accepted_oracle_package(
    *,
    candidate: CandidateEvidencePackage,
    decision: GateBDecision,
    human_revision: HumanEvidenceRevision | None = None,
) -> AcceptedOraclePackage:
    """Materialize only explicitly Gate-B-selected supporting candidate facts."""

    if candidate.task.gate_a_status != "approved":
        raise ValueError("accepted Oracle requires Gate A approval")
    if human_revision is not None:
        if decision.status != "revised":
            raise ValueError("human revision requires revised Gate B decision")
        if human_revision.source_package_sha256 != candidate_package_sha256(candidate):
            raise ValueError("human revision source package hash mismatch")
    if (
        human_revision is None
        and candidate.research_outcome is not None
        and candidate.research_outcome.status == "research_incomplete"
    ):
        raise ValueError("research_incomplete candidate cannot be accepted as an Oracle")
    source_facts = human_revision.facts if human_revision else candidate.facts
    candidates_by_id = {fact.fact_id: fact for fact in source_facts}
    if len(candidates_by_id) != len(source_facts):
        raise ValueError("candidate fact ids must be unique")
    accepted: list[CandidateEvidenceFact] = []
    for fact_id in decision.accepted_fact_ids:
        fact = candidates_by_id.get(fact_id)
        if fact is None:
            raise ValueError(f"Gate B decision refers to unknown candidate fact: {fact_id}")
        if fact.disposition != "supports":
            raise ValueError("Gate B may only adopt supporting candidate facts")
        if candidate.task.research_plan is not None and fact.requirement_id not in {
            slot.id for slot in candidate.task.research_plan.subquestions
        }:
            raise ValueError("accepted fact must reference a research plan slot")
        for evidence_ref in fact.evidence_refs:
            _validate_evidence_ref_scope(evidence_ref, candidate.task.scope)
        accepted.append(fact)
    if len({fact.fact_id for fact in accepted}) != len(accepted):
        raise ValueError("Gate B decision repeats a candidate fact")
    requirements: dict[str, list[str]] = {}
    for fact in accepted:
        requirements.setdefault(fact.requirement_id, []).append(fact.fact_id)
    return AcceptedOraclePackage(
        case_id=candidate.task.case_id,
        task_id=candidate.task.task_id,
        scope=candidate.task.scope,
        decision=decision,
        human_revision=human_revision,
        facts=[
            AcceptedOracleFact(
                fact_id=fact.fact_id,
                statement=fact.statement,
                evidence_refs=fact.evidence_refs,
                source_refs=(
                    [
                        f"human-revision://{human_revision.revision_id}/{fact.fact_id}",
                        f"candidate-sha256://{human_revision.source_package_sha256}",
                        *human_revision.evidence_source_refs,
                    ]
                    if human_revision
                    else [f"candidate-evidence://{candidate.task.task_id}/{fact.fact_id}"]
                ),
                confidence=fact.confidence,
            )
            for fact in accepted
        ],
        requirements=[
            AcceptedOracleRequirement(requirement_id=requirement_id, fact_ids=fact_ids)
            for requirement_id, fact_ids in sorted(requirements.items())
        ],
    )


def accepted_oracle_as_evidence_package(
    accepted: AcceptedOraclePackage,
) -> EvidenceOraclePackage:
    """Adapt explicitly accepted Gate-B facts to the offline quality-score port.

    This is intentionally a one-way, pure projection.  It accepts no pending
    candidate package and does not decide whether the resulting score may be
    released to an optimizer.
    """

    facts_by_id = {fact.fact_id: fact for fact in accepted.facts}
    requirements: list[EvidenceOracleRequirement] = []
    for requirement in accepted.requirements:
        accepted_facts = [facts_by_id[fact_id] for fact_id in requirement.fact_ids]
        requirements.append(
            EvidenceOracleRequirement(
                requirement_id=requirement.requirement_id,
                disposition="supports",
                gold_ids=list(requirement.fact_ids),
                accepted_facts=accepted_facts,
            )
        )
    return EvidenceOraclePackage(
        case_id=accepted.case_id,
        scope=accepted.scope,
        asset_revisions=accepted.scope.asset_revisions,
        facts=[
            EvidenceOracleFact(
                gold_id=fact.fact_id,
                disposition="supports",
                locator=_fact_locator(fact, accepted.scope),
                source_ref=source_ref,
                statement=fact.statement,
                confidence=fact.confidence,
            )
            for fact in accepted.facts
            for source_ref in fact.source_refs[:1] or [f"accepted-oracle://{fact.fact_id}"]
        ],
        requirements=requirements,
    )


def _validate_evidence_ref_scope(
    evidence_ref: dict[str, Any],
    scope: EvidenceOracleScope,
) -> None:
    library_id = str(evidence_ref.get("library_id") or evidence_ref.get("workspace") or "")
    asset_id = str(evidence_ref.get("asset_id") or evidence_ref.get("document_id") or "")
    if library_id != scope.library_id or (scope.asset_ids and asset_id not in scope.asset_ids):
        raise ValueError("candidate evidence fact is outside task scope")


def _normalize_evidence_ref(evidence_ref: dict[str, Any]) -> dict[str, Any]:
    """Flatten seed-style locators before persisting candidate evidence.

    Research seeds carry their scope under ``locator``.  Candidate evidence is
    consumed by Gate B and scoring as a locator itself, so retain the wrapper
    metadata (for example ``type``) while promoting the locator fields.
    """

    locator = evidence_ref.get("locator")
    if not isinstance(locator, dict):
        return dict(evidence_ref)
    for key in ("library_id", "workspace", "asset_id", "document_id"):
        if key in evidence_ref and key in locator and evidence_ref[key] != locator[key]:
            raise ValueError("candidate evidence locator conflicts with wrapper scope")
    return {
        **{key: value for key, value in evidence_ref.items() if key != "locator"},
        **locator,
    }


def _fact_locator(fact: AcceptedOracleFact, scope: EvidenceOracleScope) -> dict[str, Any]:
    for evidence_ref in fact.evidence_refs:
        if isinstance(evidence_ref, dict):
            return dict(evidence_ref)
    return {"library_id": scope.library_id}
