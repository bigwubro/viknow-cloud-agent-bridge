"""Unified evidence objects exchanged between retrieval and tools."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidencePartKind(str, Enum):
    """Supported evidence part kinds."""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"


class EvidenceTimeRange(BaseModel):
    """Optional time range inside a media asset."""

    start_seconds: float
    end_seconds: float


class EvidencePart(BaseModel):
    """One typed part inside a multimodal evidence object."""

    kind: EvidencePartKind
    text: str | None = None
    uri: str | None = None
    mime_type: str | None = None
    time_range: EvidenceTimeRange | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A multimodal evidence unit returned by search and consumed by tools."""

    id: str
    source: str
    title: str | None = None
    score: float | None = None
    parts: list[EvidencePart] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceLocator(BaseModel):
    """Lightweight reference to reconstruct an Evidence object from the knowledge backend."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    evidence_id: str | None = None
    title: str | None = None
    time_range: EvidenceTimeRange | None = None

    def video_parts(self) -> list[EvidencePart]:
        return [part for part in self.parts if part.kind == EvidencePartKind.VIDEO]
