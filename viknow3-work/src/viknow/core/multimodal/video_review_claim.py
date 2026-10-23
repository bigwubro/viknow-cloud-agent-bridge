"""Pydantic schemas for two-stage video review (claims extraction + self-check).

Migrated from viknow1 ``evidence_types.py`` (VideoVisualFact/Result) with
viknow2 additions for the stage-2 self-check scores and the tool-level
return contract. Field naming follows viknow1 (``claim`` / ``timestamp_*``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from viknow.core.multimodal.inspect_citation import InspectClaimRender, InspectClaimSource

# ---------------------------------------------------------------------------
# Stage 1: visual extraction (structured output schema, migrated from viknow1)
# ---------------------------------------------------------------------------


class VideoVisualFact(BaseModel):
    """Single fact produced by stage-1 VLM visual extraction."""

    claim: str
    timestamp_start: float = Field(ge=0)
    timestamp_end: float = Field(ge=0)
    entity_description: str = ""
    # Do not silently turn an omitted VLM confidence into usable evidence.
    confidence: float | None = Field(default=None, ge=0, le=1)
    time_uncertain: bool = False


class VideoVisualResult(BaseModel):
    """Stage-1 structured output: all facts extracted from one video clip."""

    facts: list[VideoVisualFact] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 2: self-check scoring (viknow2 addition)
# ---------------------------------------------------------------------------


class ClaimScore(BaseModel):
    """Stage-2 score for one stage-1 fact (1-based ``fact_index``)."""

    fact_index: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
    action: Literal["keep", "revise", "drop"] = "keep"
    corrected_claim: str | None = None
    corrected_timestamp_start: float | None = None
    corrected_timestamp_end: float | None = None


class ClaimScoreResult(BaseModel):
    """Stage-2 structured output: scores for each stage-1 fact."""

    scores: list[ClaimScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Merged claim (stage-1 fields + stage-2 self-check) and tool return contract
# ---------------------------------------------------------------------------


class VideoVisualClaim(BaseModel):
    """Final claim returned to the agent: stage-1 fields with stage-2 self-check."""

    claim_id: str = ""
    citation_index: int | None = None
    agent_citation: str = ""
    display_label: str = ""
    source: InspectClaimSource | None = None
    render: InspectClaimRender | None = None
    claim: str
    timestamp_start: float
    timestamp_end: float
    entity_description: str = ""
    confidence: float = 0.0
    time_uncertain: bool = False
    verify_reason: str = ""


class VideoReviewResult(BaseModel):
    """Tool-level return contract: ``success/error/claims``."""

    success: bool
    error: str | None = None
    claims: list[VideoVisualClaim] = Field(default_factory=list)
