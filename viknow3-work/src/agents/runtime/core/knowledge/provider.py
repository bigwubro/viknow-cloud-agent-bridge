"""Knowledge provider protocol used by ViKnow tools and adapters."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agents.runtime.core.evidence import Evidence


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


class KnowledgeSearchRequest(BaseModel):
    """Request for searching inside a specific knowledge library."""

    library_id: str
    query: str
    top_k: int = Field(default=4, ge=1)
    filters: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchResult(BaseModel):
    """Search result returned by a knowledge provider."""

    library_id: str
    query: str
    evidence: list[Evidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeProvider(Protocol):
    """Boundary implemented by an external knowledge repository."""

    def list_libraries(
        self, *, library_ids: Collection[str] | None = None
    ) -> list[KnowledgeLibrary]: ...

    def search_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        score_threshold: float = 0.5,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]: ...

    def discover_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        library_ids: Collection[str] | None = None,
    ) -> list[KnowledgeLibrarySearchResult]: ...

    def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResult: ...
