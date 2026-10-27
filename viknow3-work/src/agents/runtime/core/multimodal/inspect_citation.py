"""Shared citation metadata returned by inspect tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class InspectClaimSource(BaseModel):
    """Machine-readable source locator for one inspect claim."""

    type: Literal["text", "image", "video"]
    library_id: str | None = None
    asset_id: str | None = None
    asset_path: str | None = None
    evidence_id: str | None = None
    title: str | None = None
    segment_id: str | None = None
    source_image_id: str | None = None
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    uri: str | None = None
    anchors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InspectClaimRender(BaseModel):
    """Frontend-oriented rendering hints for one inspect claim."""

    kind: Literal["text_quote", "image", "video_clip"]
    label: str
    url: str | None = None
    thumbnail_url: str | None = None
    text_quote: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
