"""Human review contracts for the learning candidate gate."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from viknow.core.eval.analysis import RunAnalysisResult

CandidateStatus = Literal["pending", "approved", "rejected", "removed"]
ReviewStatus = Literal["pending", "approved", "rejected"]
CandidateSource = Literal["manual", "evaluator"]
CandidateRisk = Literal["low", "medium", "high"]
ProposalStatus = Literal["queued", "running", "ready", "failed", "cancelled"]
AnalysisTaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
SkillOptimizationTaskStatus = Literal[
    "queued", "running", "pending_acceptance", "accepted", "rejected", "failed", "cancelled"
]
SkillOptimizationExperimentStatus = SkillOptimizationTaskStatus
ACTIVE_SKILL_OPTIMIZATION_STATUSES: frozenset[SkillOptimizationTaskStatus] = frozenset(
    {"queued", "running", "pending_acceptance"}
)
OracleRequestStatus = Literal[
    "pending_scope_resolution",
    "queued",
    "running",
    "pending_gate_b",
    "accepted",
    "revised",
    "rejected",
    "needs_more_evidence",
    "failed",
    "cancelled",
]
HandlingDirection = Literal[
    "skill_instruction",
    "harness_policy",
    "tool_or_index",
    "knowledge_source",
    "model_behavior",
    "product_ui",
    "no_action",
    "uncertain",
]


class CandidateCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)
    question: str | None = Field(default=None, max_length=20_000)
    status: CandidateStatus = "pending"
    source: CandidateSource
    bucket: Literal["good", "bad", "uncertain"] | None = None
    reason_codes: list[str] = Field(default_factory=list)
    added_by: str = Field(min_length=1, max_length=128)
    added_at: datetime
    updated_at: datetime
    current_reason: str | None = Field(default=None, max_length=2000)
    reference_answer: str | None = Field(default=None, max_length=20_000)
    active: bool = True


class CaseReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    status: CandidateStatus
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
    created_at: datetime


class ApprovedCaseSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    run_id: str = Field(min_length=1, max_length=128)
    question: str
    answer: str | None = None
    behavior_snapshot: dict = Field(default_factory=dict)
    citations: dict = Field(default_factory=dict)
    source_event_sequences: list[int] = Field(default_factory=list)
    optimization_proposal: OptimizationProposal | None = None
    analysis_task_id: str | None = None
    analysis_attempts: int | None = Field(default=None, ge=0)
    created_at: datetime


class OptimizationProposalAnalysis(BaseModel):
    """LLM-authored diagnosis; deterministic classification remains outside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_summary: str = Field(min_length=1, max_length=2000)
    handling_direction: HandlingDirection
    target_skill_id: str | None = Field(default=None, min_length=1, max_length=128)
    optimization_suggestion: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    requires_oracle: bool = Field(
        default=False,
        description="Whether optional material research is recommended before optimization.",
    )
    oracle_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="Why optional material research would improve the optimization input.",
    )
    trace_analysis: RunAnalysisResult | None = None

    @model_validator(mode="after")
    def validate_target_skill(self) -> OptimizationProposalAnalysis:
        if self.handling_direction != "skill_instruction" and self.target_skill_id is not None:
            raise ValueError("target_skill_id is only valid for skill_instruction")
        if self.requires_oracle != (self.oracle_reason is not None):
            raise ValueError("oracle_reason is required exactly when requires_oracle is true")
        return self


class OptimizationProposalAnalysisInput(BaseModel):
    """Bounded run content supplied to the semantic analyzer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    question: str
    answer: str | None = None
    bucket: Literal["good", "bad", "uncertain"]
    risk: CandidateRisk
    reason_codes: list[str] = Field(default_factory=list)
    answer_review: dict = Field(default_factory=dict)
    citation_count: int = Field(ge=0)


class AnswerReviewReplayOutcome(BaseModel):
    """Supplemental review produced for analysis without mutating the source run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed"] = "completed"
    original_error_code: Literal["answer_review_timeout"] = "answer_review_timeout"
    reviewer_model: str | None = None
    result: dict


class OptimizationProposal(BaseModel):
    """Durable proposal combining rule-owned facts and an optional LLM analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    rule_version: Literal["learning-candidate-v1"] = "learning-candidate-v1"
    run_id: str = Field(min_length=1, max_length=128)
    status: ProposalStatus
    bucket: Literal["good", "bad", "uncertain"]
    risk: CandidateRisk
    reason_codes: list[str] = Field(default_factory=list)
    analysis: OptimizationProposalAnalysis | None = None
    analysis_model: str | None = None
    answer_review_replay: AnswerReviewReplayOutcome | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class OptimizationAnalysisTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    status: AnalysisTaskStatus
    attempts: int = Field(ge=0)
    error_code: str | None = None
    activity_kind: Literal["think", "tool", "error"] | None = None
    activity_text: str | None = Field(default=None, max_length=4000)
    lease_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OracleRequest(BaseModel):
    """Durable bridge from an approved learning case to a scoped Oracle run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(min_length=1, max_length=128)
    gate_a_decision_ref: str = Field(min_length=1, max_length=256)
    status: OracleRequestStatus
    library_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    oracle_reason: str = Field(min_length=1, max_length=2000)
    evidence_collection_task: dict | None = None
    oracle_run_id: str | None = None
    candidate_evidence_package: dict | None = None
    gate_b_review: OracleGateBReview | None = None
    accepted_oracle_package: dict | None = None
    error_code: str | None = None
    attempts: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OracleGateBReview(BaseModel):
    """Auditable human conclusion over one immutable candidate evidence package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "revised", "rejected", "needs_more_evidence"]
    decision_ref: str = Field(min_length=1, max_length=256)
    reviewer: str = Field(min_length=1, max_length=128)
    accepted_fact_ids: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)
    created_at: datetime

    @model_validator(mode="after")
    def validate_selected_facts(self) -> OracleGateBReview:
        accepted = self.status in {"accepted", "revised"}
        if accepted and not self.accepted_fact_ids:
            raise ValueError("accepted Gate B review requires selected facts")
        if not accepted and self.accepted_fact_ids:
            raise ValueError("non-accepted Gate B review cannot select facts")
        return self


class SkillOptimizationExperimentConfig(BaseModel):
    """Complete non-secret configuration frozen when a batch experiment starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    advanced_defaults: ClassVar[dict[str, JsonValue]] = {
        "analyst_max_completion_tokens": 16384,
        "analyst_workers": 1,
        "edit_budget": 1,
        "failure_only": False,
        "longitudinal_pair_policy": "mixed",
        "lr_control_mode": "fixed",
        "lr_scheduler": "constant",
        "max_analyst_rounds": 3,
        "merge_batch_size": 1,
        "min_edit_budget": 1,
        "minibatch_size": 1,
        "reasoning_effort": "none",
        "rewrite_max_completion_tokens": 64000,
        "rewrite_reasoning_effort": "high",
        "seed": 42,
        "selection_disable_cache": False,
        "skill_aware_appendix_source": "both",
        "skill_aware_consolidate_threshold": 5,
        "skill_update_mode": "patch",
        "slow_update_gate_with_selection": True,
        "slow_update_samples": 20,
        "use_meta_skill": False,
        "use_skill_aware_reflection": False,
        "use_slow_update": False,
    }
    advanced_parameter_names: ClassVar[frozenset[str]] = frozenset(advanced_defaults)

    optimizer_model: str = Field(min_length=1, max_length=256)
    target_model: str = Field(min_length=1, max_length=256)
    evaluator_model: str = Field(min_length=1, max_length=256)
    epochs: int = Field(default=1, ge=1, le=100)
    batch_size: int = Field(default=1, ge=1, le=100)
    n_samples: int = Field(default=1, ge=1, le=100)
    acceptance_metric: Literal["hard", "soft"] = "soft"
    acceptance_min_delta: float = Field(default=0.0, ge=0, le=1)
    advanced_parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_advanced_parameters(self) -> SkillOptimizationExperimentConfig:
        sensitive_fragments = {
            "api_key",
            "base_url",
            "endpoint",
            "environment",
            "password",
            "secret",
            "token",
        }

        def visit(value: JsonValue, path: str) -> None:
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"advanced parameter {path} must be finite")
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = key.strip().lower()
                    if normalized not in self.advanced_parameter_names and any(
                        fragment in normalized for fragment in sensitive_fragments
                    ):
                        raise ValueError("secret, URL, and environment parameters may not persist")
                    visit(nested, f"{path}.{key}")
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    visit(nested, f"{path}[{index}]")

        visit(self.advanced_parameters, "advanced_parameters")
        unsupported = self.advanced_parameters.keys() - self.advanced_parameter_names
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported advanced parameter: {names}")
        boolean_names = {
            "failure_only",
            "selection_disable_cache",
            "slow_update_gate_with_selection",
            "use_meta_skill",
            "use_skill_aware_reflection",
            "use_slow_update",
        }
        choices = {
            "longitudinal_pair_policy": {"mixed", "changed", "unchanged"},
            "lr_control_mode": {"fixed", "autonomous", "none"},
            "lr_scheduler": {"constant", "linear", "cosine"},
            "reasoning_effort": {"none", "minimal", "low", "medium", "high", "xhigh"},
            "rewrite_reasoning_effort": {"none", "minimal", "low", "medium", "high", "xhigh"},
            "skill_aware_appendix_source": {"both", "failure_only", "appendix_only"},
            "skill_update_mode": {"patch", "rewrite"},
        }
        for name, value in self.advanced_parameters.items():
            if name in boolean_names:
                valid = type(value) is bool
            elif name in choices:
                valid = isinstance(value, str) and value in choices[name]
            else:
                maximum = (
                    2**31 - 1
                    if name == "seed"
                    else (131072 if name.endswith("completion_tokens") else 100)
                )
                minimum = 0 if name in {"seed", "skill_aware_consolidate_threshold"} else 1
                valid = type(value) is int and minimum <= value <= maximum
            if not valid:
                raise ValueError(f"advanced parameter {name} has an invalid or out-of-bounds value")
        object.__setattr__(
            self, "advanced_parameters", {**self.advanced_defaults, **self.advanced_parameters}
        )
        return self


class SkillOptimizationExperimentCase(BaseModel):
    """Immutable case material captured inside an experiment transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)
    reference_answer: str | None = Field(default=None, max_length=20_000)
    gate_a_snapshot: ApprovedCaseSnapshot
    oracle_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    accepted_oracle_package: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_oracle_material(self) -> SkillOptimizationExperimentCase:
        if (self.oracle_request_id is None) != (self.accepted_oracle_package is None):
            raise ValueError(
                "accepted Oracle package and Oracle request id must be frozen together"
            )
        if self.gate_a_snapshot.run_id != self.run_id:
            raise ValueError("Gate A snapshot run id differs from experiment case")
        return self


class SkillOptimizationAcceptanceReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["keep", "reject"]
    reviewer: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)
    created_at: datetime


class SkillOptimizationExperiment(BaseModel):
    """One durable SkillOpt run over one or more approved cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=128)
    target_skill_id: Literal["knowledge-qa-v2"] = "knowledge-qa-v2"
    status: SkillOptimizationExperimentStatus = "queued"
    config: SkillOptimizationExperimentConfig
    cases: list[SkillOptimizationExperimentCase] = Field(min_length=1)
    attempts: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    artifact_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    progress: dict[str, JsonValue] = Field(default_factory=dict)
    result_summary: dict[str, JsonValue] = Field(default_factory=dict)
    acceptance_manifest: dict[str, JsonValue] = Field(default_factory=dict)
    acceptance_review: SkillOptimizationAcceptanceReview | None = None
    error_code: str | None = None
    error_summary: str | None = Field(default=None, max_length=2000)
    created_by: str = Field(min_length=1, max_length=128)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_unique_cases(self) -> SkillOptimizationExperiment:
        run_ids = [case.run_id for case in self.cases]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("experiment contains duplicate run id")
        return self


class SkillOptimizationTask(BaseModel):
    """Durable, non-publishing SkillOpt experiment and acceptance record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(min_length=1, max_length=128)
    input_source: Literal["gate_a_snapshot", "accepted_oracle"] = "accepted_oracle"
    oracle_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_skill_id: str = Field(min_length=1, max_length=128)
    reference_answer: str | None = Field(default=None, max_length=20_000)
    status: SkillOptimizationTaskStatus
    attempts: int = Field(default=0, ge=0)
    error_code: str | None = None
    lease_until: datetime | None = None
    experiment_id: str | None = None
    result_summary: dict | None = None
    acceptance_manifest: dict | None = None
    acceptance_review: SkillOptimizationAcceptanceReview | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_acceptance_boundary(self) -> SkillOptimizationTask:
        if self.input_source == "accepted_oracle" and self.oracle_request_id is None:
            raise ValueError("accepted Oracle input requires an Oracle request")
        if self.input_source == "gate_a_snapshot" and self.oracle_request_id is not None:
            raise ValueError("Gate A snapshot input cannot reference an Oracle request")
        if (
            self.acceptance_manifest is not None
            and self.acceptance_manifest.get("release_eligible") is not False
        ):
            raise ValueError("self-learning optimization may not authorize release")
        if self.acceptance_manifest is not None:
            manifest = self.acceptance_manifest
            if manifest.get("case_id") != self.source_run_id:
                raise ValueError("optimization manifest case_id differs from its source run")
            if self.experiment_id is None or manifest.get("experiment_id") != self.experiment_id:
                raise ValueError("optimization manifest experiment_id differs from its task")
            if self.input_source == "gate_a_snapshot":
                if (
                    manifest.get("schema_version") != "viknow.skillopt-gate-a-acceptance.v1"
                    or manifest.get("evaluation_stage") != "provisional"
                ):
                    raise ValueError("Gate A optimization requires a provisional manifest")
            else:
                comparison = manifest.get("comparison")
                if (
                    manifest.get("schema_version") != "viknow.skillopt-first-acceptance.v1"
                    or not manifest.get("oracle_storage_key")
                    or not isinstance(comparison, dict)
                    or comparison.get("evaluation_stage") != "accepted"
                ):
                    raise ValueError("Oracle optimization requires an accepted-stage manifest")
        if self.status == "pending_acceptance" and self.acceptance_manifest is None:
            raise ValueError("pending acceptance requires an acceptance manifest")
        if self.status in {"accepted", "rejected"} and self.acceptance_review is None:
            raise ValueError("terminal acceptance status requires a human review")
        return self
