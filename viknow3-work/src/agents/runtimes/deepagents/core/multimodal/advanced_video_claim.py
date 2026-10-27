"""Structured contracts for single-video adaptive inspection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.runtimes.deepagents.core.multimodal.inspect_citation import InspectClaimRender, InspectClaimSource


class NormalizedCrop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> NormalizedCrop:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop must stay inside normalized frame bounds")
        return self


class AdvancedVideoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_budget: Literal["low", "balanced", "high"] = "balanced"
    instruction: str | None = None


class AdvancedVideoClipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(min_length=1)
    fps: Literal["auto"] | float = "auto"
    crop: NormalizedCrop | None = None

    @model_validator(mode="after")
    def validate_fps(self) -> AdvancedVideoClipInput:
        if isinstance(self.fps, float) and not 0.1 <= self.fps <= 8:
            raise ValueError("fps must be between 0.1 and 8")
        return self


class RefinementRequest(BaseModel):
    clip_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    fps: float = Field(default=1, ge=0.1, le=8)
    crop: NormalizedCrop | None = None
    crop_confidence: float = Field(default=0, ge=0, le=1)
    grounding_target: str = ""
    reason: str = ""


class CoarseObservation(BaseModel):
    claim: str
    supporting_frame_ids: list[str] = Field(default_factory=list)
    # Missing confidence must remain observable; downstream evidence gating rejects it.
    confidence: float | None = Field(default=None, ge=0, le=1)
    verify_reason: str = ""


class AdvancedVideoPlan(BaseModel):
    observations: list[CoarseObservation] = Field(default_factory=list)
    refinement: RefinementRequest | None = None


class RefinedObservation(BaseModel):
    observation_id: str
    clip_id: str
    claim: str
    timestamp_start: float
    timestamp_end: float
    confidence: float = Field(ge=0, le=1)
    verify_reason: str = ""
    fps: float
    crop: NormalizedCrop | None = None
    stage: Literal["coarse", "refined"] = "refined"
    frame_ids: list[str] = Field(default_factory=list)


class AdvancedClaimSupport(BaseModel):
    observation_id: str = ""
    stage: Literal["coarse", "refined"] = "refined"
    frame_ids: list[str] = Field(default_factory=list)
    clip_id: str
    timestamp_start: float
    timestamp_end: float
    confidence: float = Field(ge=0, le=1)
    fps: float
    crop: NormalizedCrop | None = None


class AdvancedVideoClaim(BaseModel):
    claim_id: str = ""
    citation_index: int | None = None
    agent_citation: str = ""
    display_label: str = ""
    source: InspectClaimSource | None = None
    sources: list[InspectClaimSource] = Field(default_factory=list)
    render: InspectClaimRender | None = None
    claim: str
    supporting_observation_ids: list[str] = Field(default_factory=list)
    source_clip_ids: list[str] = Field(default_factory=list)
    supports: list[AdvancedClaimSupport] = Field(default_factory=list)
    timestamp_start: float
    timestamp_end: float
    confidence: float = Field(default=0, ge=0, le=1)
    verify_reason: str = ""


class AdvancedClipStatus(BaseModel):
    clip_id: str
    asset_id: str | None = None
    evidence_id: str
    status: Literal["completed", "failed"]
    error: str | None = None
    requested_fps: str | float = "auto"
    actual_fps: list[float] = Field(default_factory=list)
    sampled_timestamps: list[float] = Field(default_factory=list)
    crop: NormalizedCrop | None = None
    crop_fallback: bool = False
    grounding: dict | None = None
    coarse_max_edge: int | None = None
    refined_max_edge: int | None = None
    vlm_video_options: dict[str, object] = Field(default_factory=dict)
    submitted_bytes: int = 0


class AdvancedVideoReviewResult(BaseModel):
    success: bool
    error: str | None = None
    claims: list[AdvancedVideoClaim] = Field(default_factory=list)
    video: AdvancedClipStatus | None = None
