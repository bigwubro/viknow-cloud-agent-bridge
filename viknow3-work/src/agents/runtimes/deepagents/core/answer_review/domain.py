"""Answer review domain contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.runtimes.deepagents.core.answer_review.choice import ChoiceOption, ChoiceSelection
from agents.runtimes.deepagents.core.answer_review.slot_coverage import SlotAssessment
from agents.runtimes.deepagents.core.evidence import Evidence

# runtime 门控反馈工具名（stop_gate_check）。它不是证据工具、不产出 claim，
# 消费端按“证据/核验工具”语义枚举本轮工具时应排除，避免混入工具名清单。
STOP_GATE_CHECK_TOOL_NAME = "stop_gate_check"


class ClaimSupportStatus(StrEnum):
    """Support state for one answer claim."""

    SUPPORTED = "supported"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"


class RiskSeverity(StrEnum):
    """Severity levels for answer review risks."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnswerReviewDimensionStatus(StrEnum):
    """Whether a dimension score is meaningful for one reviewed answer."""

    SCORED = "scored"
    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUABLE = "not_evaluable"


class CitationPolicyMode(StrEnum):
    """Whether citations are expected for one reviewed answer."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_EXPECTED = "not_expected"
    REFUSAL_NO_EVIDENCE = "refusal_no_evidence"


class AnswerReviewConfig(BaseModel):
    """Per-profile answer review gate settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    gate_enabled: bool = False
    targeted_repair_enabled: bool = False
    distinguish_execution_failures: bool = False
    # V4: the claim/semantic sub-reviewers and the policy classifier are
    # optional. Disabled means the review still runs rule-only and records its
    # execution state, but issues no extra provider calls.
    llm_reviewers_enabled: bool = True
    pass_threshold: int = Field(default=75, ge=0, le=100)
    max_retries: int = Field(default=0, ge=0, le=3)
    allow_low_confidence_output: bool = True
    timeout_seconds: float = Field(default=20, gt=0)


class AnswerReviewDimensionScores(BaseModel):
    """Dimension scores emitted by an answer reviewer."""

    model_config = ConfigDict(extra="forbid")

    citation_validity: int = Field(ge=0, le=100)
    claim_support: int = Field(ge=0, le=100)
    contradiction: int = Field(ge=0, le=100)
    coverage: int = Field(ge=0, le=100)


class AnswerReviewDimensionStatuses(BaseModel):
    """Applicability status for each answer review dimension."""

    model_config = ConfigDict(extra="forbid")

    citation_validity: AnswerReviewDimensionStatus = AnswerReviewDimensionStatus.SCORED
    claim_support: AnswerReviewDimensionStatus = AnswerReviewDimensionStatus.SCORED
    contradiction: AnswerReviewDimensionStatus = AnswerReviewDimensionStatus.SCORED
    coverage: AnswerReviewDimensionStatus = AnswerReviewDimensionStatus.SCORED


class AnswerReviewClaim(BaseModel):
    """One claim extracted from a draft answer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    support_status: ClaimSupportStatus
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_details: list[dict[str, Any]] = Field(default_factory=list)
    span_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class AnswerReviewSpan(BaseModel):
    """One answer text span associated with nearby citation markers."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    citation_markers: list[str] = Field(default_factory=list)
    cited_claim_refs: list[str] = Field(default_factory=list)


class AnswerReviewClaimRelevancePair(BaseModel):
    """One answer-span and cited-claim pair sent to a relevance scorer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    span_id: str
    citation_marker: str
    answer_span: str
    cited_claim: str
    cited_claim_ref: str


class AnswerReviewClaimRelevanceScore(BaseModel):
    """Relevance score for one answer-span and cited-claim pair."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str
    score: float = Field(ge=0, le=1)


class AnswerReviewRisk(BaseModel):
    """One review issue or risk surfaced to the user."""

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: RiskSeverity
    kind: str
    message: str
    evidence_ids: list[str] = Field(default_factory=list)
    slot_ids: list[str] = Field(default_factory=list)
    conflicting_claim_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    repair_action: Literal["none", "revise_answer", "inspect", "locate_adjacent"] = "none"


class AnswerReviewCitation(BaseModel):
    """One answer citation resolved to a tool claim."""

    model_config = ConfigDict(extra="forbid")

    marker: str
    claim_id: str = ""
    citation_index: int | None = None
    tool: str = ""
    claim: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    quote_verified: bool | None = None
    verify_reason: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    render: dict[str, Any] = Field(default_factory=dict)
    claim_ref: str = ""


class AnswerReviewCitationPolicy(BaseModel):
    """Runtime-derived citation requirement for one draft answer."""

    model_config = ConfigDict(extra="forbid")

    mode: CitationPolicyMode = CitationPolicyMode.REQUIRED
    reason: str = "default"
    evidence_required: bool = True
    tool_names: list[str] = Field(default_factory=list)


class AnswerReviewPolicyClassification(BaseModel):
    """Optional LLM-derived citation policy suggestion for gray-area answers."""

    model_config = ConfigDict(extra="forbid")

    mode: CitationPolicyMode
    reason: str
    evidence_required: bool
    confidence: float = Field(ge=0, le=1)


class AnswerReviewPolicyClassificationRequest(BaseModel):
    """Inputs sent to an optional policy classifier."""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    tool_names: list[str] = Field(default_factory=list)
    search_evidence_count: int = Field(default=0, ge=0)
    claim_count: int = Field(default=0, ge=0)
    citation_count: int = Field(default=0, ge=0)
    rule_policy: AnswerReviewCitationPolicy


class AnswerReviewRequest(BaseModel):
    """Inputs used to review one draft answer."""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_relations: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[AnswerReviewCitation] = Field(default_factory=list)
    citation_errors: list[str] = Field(default_factory=list)
    citation_policy: AnswerReviewCitationPolicy = Field(default_factory=AnswerReviewCitationPolicy)
    answer_plan: dict[str, Any] | None = None
    attempt: int = Field(default=1, ge=1)
    previous_feedback: str | None = None
    choice_options: list[ChoiceOption] = Field(default_factory=list)
    answer_slots: list[dict[str, Any]] = Field(default_factory=list)
    evidence_facts: list[dict[str, Any]] = Field(default_factory=list)


class AnswerReviewReplayInput(BaseModel):
    """Versioned, secret-free input needed to rerun one answer review."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["answer-review-input.v1"] = "answer-review-input.v1"
    request: AnswerReviewRequest
    config: AnswerReviewConfig
    reviewer_model: str | None = None


class AnswerReviewClaimReviewRequest(BaseModel):
    """Inputs sent to an optional claim extraction and citation coverage reviewer."""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    spans: list[AnswerReviewSpan] = Field(default_factory=list)
    citations: list[AnswerReviewCitation] = Field(default_factory=list)
    citation_policy: AnswerReviewCitationPolicy


class AnswerReviewSemanticReviewRequest(BaseModel):
    """Inputs sent to an optional semantic consistency and intent reviewer."""

    model_config = ConfigDict(extra="forbid")

    choice_options: list[ChoiceOption] = Field(default_factory=list)
    answer_slots: list[dict[str, Any]] = Field(default_factory=list)
    evidence_facts: list[dict[str, Any]] = Field(default_factory=list)
    question: str
    answer: str
    spans: list[AnswerReviewSpan] = Field(default_factory=list)
    claims: list[AnswerReviewClaim] = Field(default_factory=list)
    citation_policy: AnswerReviewCitationPolicy


class AnswerReviewSemanticReview(BaseModel):
    """Optional LLM-derived contradiction and coverage review result."""

    model_config = ConfigDict(extra="forbid")

    contradiction_score: int | None = Field(default=None, ge=0, le=100)
    coverage_score: int | None = Field(default=None, ge=0, le=100)
    risks: list[AnswerReviewRisk] = Field(default_factory=list)
    selected_choice: ChoiceSelection | None = None
    slot_assessments: list[SlotAssessment] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    intent_units: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""


class AnswerReviewClaimReview(BaseModel):
    """Optional LLM-derived claim review result."""

    model_config = ConfigDict(extra="forbid")

    claims: list[AnswerReviewClaim] = Field(default_factory=list)
    risks: list[AnswerReviewRisk] = Field(default_factory=list)
    claim_support_score: int | None = Field(default=None, ge=0, le=100)
    coverage_score: int | None = Field(default=None, ge=0, le=100)


class AnswerReviewResult(BaseModel):
    """Structured answer review result consumed by Runtime and UI."""

    model_config = ConfigDict(extra="forbid")

    execution_status: (
        Literal["completed", "unavailable", "invalid_input", "invalid_output"] | None
    ) = None
    overall_score: int = Field(ge=0, le=100)
    passed: bool
    retryable: bool
    dimension_scores: AnswerReviewDimensionScores
    dimension_statuses: AnswerReviewDimensionStatuses = Field(
        default_factory=AnswerReviewDimensionStatuses
    )
    summary: str
    retry_instruction: str
    citation_policy: AnswerReviewCitationPolicy = Field(default_factory=AnswerReviewCitationPolicy)
    claims: list[AnswerReviewClaim] = Field(default_factory=list)
    risks: list[AnswerReviewRisk] = Field(default_factory=list)
    citations: dict[str, dict] = Field(default_factory=dict)
    selected_choice: ChoiceSelection | None = None
    slot_assessments: list[SlotAssessment] = Field(default_factory=list)
    review_trace: dict[str, Any] = Field(default_factory=dict)
