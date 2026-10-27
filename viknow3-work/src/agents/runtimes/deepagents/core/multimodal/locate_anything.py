"""LocateAnything result models and the client port tools call."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class LocateAnythingDetection(BaseModel):
    """One visual grounding detection."""

    label: str | None = None
    box: list[float] | None = None
    point: list[float] | None = None
    score: float | None = None
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocateAnythingResult(BaseModel):
    """Normalized LocateAnything response for ViKnow tools."""

    request_id: str
    query: str
    detections: list[LocateAnythingDetection] = Field(default_factory=list)
    image_width: int | None = None
    image_height: int | None = None
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocateAnythingClient(Protocol):
    """Port implemented by Actio LocateAnything client adapters."""

    def locate_image(
        self,
        media_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult: ...

    def recent_requests(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

