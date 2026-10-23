"""Pydantic schemas for text evidence inspection (single-stage LLM + fuzzy match validation).

Unlike video/image inspect, text_inspect uses a single LLM call for extraction
followed by deterministic fuzzy-match verification against the source text.

Field naming follows the text context (``source_segments`` / ``supporting_quote``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from viknow.core.multimodal.inspect_citation import InspectClaimRender, InspectClaimSource


class TextEvidenceSpan(BaseModel):
    """One quoted text span supporting a text inspect claim."""

    segment_id: str
    supporting_quote: str
    quote_verified: bool = False
    match_ratio: float = 0.0
    verify_reason: str = ""


class TextInspectClaim(BaseModel):
    """Single claim extracted from text evidence with verification results."""

    claim_id: str = ""
    citation_index: int | None = None
    agent_citation: str = ""
    display_label: str = ""
    source: InspectClaimSource | None = None
    render: InspectClaimRender | None = None
    claim: str
    source_segments: list[str] = Field(default_factory=list)
    supporting_quote: str = ""
    evidence_spans: list[TextEvidenceSpan] = Field(default_factory=list)
    verified_source_segments: list[str] = Field(default_factory=list)
    unverified_source_segments: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.0)
    quote_verified: bool = False
    match_ratio: float = 0.0
    verify_reason: str = ""


class TextInspectResult(BaseModel):
    """Tool-level return contract: ``success/error/claims``."""

    success: bool
    error: str | None = None
    claims: list[TextInspectClaim] = Field(default_factory=list)
