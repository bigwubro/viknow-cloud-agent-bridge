"""Library records returned by the HyperRAG engine adapter."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KnowledgeLibrary(BaseModel):
    """One searchable knowledge library exposed to ViKnow."""

    id: str
    name: str
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeAssetDiscovery(BaseModel):
    """One bounded document or video match used to select a knowledge library."""

    asset_id: str
    title: str
    modality: str
    summary: str
    score: float | None = None
    match_reasons: list[str] = Field(default_factory=list)


class KnowledgeLibrarySearchResult(BaseModel):
    """One candidate library returned by library discovery."""

    library: KnowledgeLibrary
    score: float | None = None
    reason: str | None = None
    asset_matches: list[KnowledgeAssetDiscovery] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
