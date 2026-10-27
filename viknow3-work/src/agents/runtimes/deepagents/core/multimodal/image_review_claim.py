"""Pydantic schemas for two-stage image review (claims extraction + self-check).

Mirrors video_review_claim.py but for images. Field naming follows the image
context (``claim`` / ``source_image_id`` instead of timestamp-based fields).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.runtimes.deepagents.core.multimodal.inspect_citation import InspectClaimRender, InspectClaimSource

# ---------------------------------------------------------------------------
# Stage 1: visual extraction
# ---------------------------------------------------------------------------


class ImageVisualFact(BaseModel):
    """Single fact produced by stage-1 VLM visual extraction."""

    claim: str
    source_image_id: str = ""
    confidence: float = 0.5


class ImageVisualResult(BaseModel):
    """Stage-1 structured output: all facts extracted from one image."""

    facts: list[ImageVisualFact] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 2: self-check scoring
# ---------------------------------------------------------------------------


class ImageClaimScore(BaseModel):
    """Stage-2 score for one stage-1 fact (1-based ``fact_index``)."""

    fact_index: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class ImageClaimScoreResult(BaseModel):
    """Stage-2 structured output: scores for each stage-1 fact."""

    scores: list[ImageClaimScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Merged claim and tool return contract
# ---------------------------------------------------------------------------


class ImageVisualClaim(BaseModel):
    """Final claim returned to the agent: stage-1 fields with stage-2 self-check."""

    claim_id: str = ""
    citation_index: int | None = None
    agent_citation: str = ""
    display_label: str = ""
    source: InspectClaimSource | None = None
    render: InspectClaimRender | None = None
    claim: str
    source_image_id: str = ""
    confidence: float = 0.0
    verify_reason: str = ""


class ImageReviewResult(BaseModel):
    """Tool-level return contract: ``success/error/claims``."""

    success: bool
    error: str | None = None
    claims: list[ImageVisualClaim] = Field(default_factory=list)
