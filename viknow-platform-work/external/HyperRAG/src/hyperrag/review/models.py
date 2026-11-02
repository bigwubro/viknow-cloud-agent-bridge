"""Structured contracts shared by index quality reviewers."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReviewStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNREVIEWED = "unreviewed"


class NormalizedChunk(BaseModel):
    chunk_id: str
    asset_id: str
    order: int
    content: str
    raw_content: str
    token_count: int | None = None
    segment_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RuleFinding(BaseModel):
    code: str
    severity: ReviewStatus
    message: str
    evidence: str | None = None


class ChunkBoundaryAnalysis(BaseModel):
    chunk_id: str
    status: ReviewStatus
    heuristic_score: float = Field(ge=0.0, le=1.0)
    findings: list[RuleFinding] = Field(default_factory=list)


class SourceInformationUnit(BaseModel):
    unit_id: str
    kind: str
    locator: dict[str, Any] = Field(default_factory=dict)
    content: str
    weight: float = Field(default=1.0, gt=0.0)
    reviewable: bool = True
    risk_codes: list[str] = Field(default_factory=list)


class ParsedEvidence(BaseModel):
    segment_id: str
    kind: str
    content: str
    locator: dict[str, Any] = Field(default_factory=dict)
    generated: bool = False


class RetentionFinding(BaseModel):
    source_unit: SourceInformationUnit
    status: ReviewStatus
    score: float = Field(ge=0.0, le=1.0)
    matched_segment_ids: list[str] = Field(default_factory=list)
    reason: str


class ParseRetentionReview(BaseModel):
    status: ReviewStatus
    information_retention: float | None = Field(default=None, ge=0.0, le=1.0)
    reviewed_weight: float = Field(ge=0.0)
    unreviewed_weight: float = Field(ge=0.0)
    findings: list[RetentionFinding] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)


class SemanticCompletenessJudgment(BaseModel):
    semantic_completeness: float = Field(ge=0.0, le=1.0)
    standalone_understandability: float = Field(ge=0.0, le=1.0)
    boundary_integrity: float = Field(ge=0.0, le=1.0)
    missing_context_types: list[str] = Field(default_factory=list)
    bad_span: str | None = None
    reason: str


class ChunkCompletenessReview(BaseModel):
    chunk: NormalizedChunk
    status: ReviewStatus
    semantic_score: float | None = Field(default=None, ge=0.0, le=1.0)
    boundary_analysis: ChunkBoundaryAnalysis
    judgment: SemanticCompletenessJudgment | None = None
    error: str | None = None


class AssetReviewData(BaseModel):
    workspace: str
    asset_id: str
    source_path: str
    parse_mode: str
    asset: dict[str, Any] = Field(default_factory=dict)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    chunks: list[NormalizedChunk] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IndexQualityReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: str
    workspace: str
    asset_id: str
    source_path: str
    source_sha256: str
    status: ReviewStatus
    parse_review: ParseRetentionReview
    chunk_reviews: list[ChunkCompletenessReview] = Field(default_factory=list)
    chunk_macro_score: float | None = Field(default=None, ge=0.0, le=1.0)
    chunk_token_weighted_score: float | None = Field(default=None, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    review_config: dict[str, Any] = Field(default_factory=dict)
