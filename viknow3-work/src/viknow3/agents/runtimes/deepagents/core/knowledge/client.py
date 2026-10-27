"""Knowledge capability a turn calls. The shell owns the engine and injects this client."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any, Protocol


class KnowledgeSearchResult(Protocol):
    """Fields tools read from a knowledge search."""

    evidence: list[Any]
    metadata: dict[str, Any]


class KnowledgeClient(Protocol):
    """Injected knowledge engine. The runtime does not construct it."""

    default_library_id: str

    def list_libraries(self, *, library_ids: Collection[str] | None = None) -> list[Any]: ...

    async def list_libraries_async(
        self, *, library_ids: Collection[str] | None = None
    ) -> list[Any]: ...

    def discover_libraries(
        self,
        query: str,
        *,
        top_k: int | None = None,
        library_ids: Collection[str] | None = None,
    ) -> list[Any]: ...

    async def discover_libraries_async(
        self,
        query: str,
        *,
        top_k: int | None = None,
        library_ids: Collection[str] | None = None,
    ) -> list[Any]: ...

    def search(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> KnowledgeSearchResult: ...

    async def search_async(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> KnowledgeSearchResult: ...

    def search_by_image(
        self,
        *,
        library_id: str,
        image_data_url: str,
        query: str = "",
        mix_search: bool = True,
        top_k: int | None = None,
    ) -> KnowledgeSearchResult: ...

    async def query_kg_async(
        self,
        *,
        library_id: str,
        query: str,
        pre_compile: bool,
        top_k: int,
        threshold: float,
    ) -> dict[str, list[dict[str, str]]]: ...

    def list_asset_segments(self, *, library_id: str, asset_id: str) -> list[dict[str, Any]]: ...

    def get_chunk_by_id(self, *, library_id: str, evidence_id: str) -> dict[str, Any] | None: ...

    def get_asset_status(self, *, library_id: str, asset_id: str) -> Any | None: ...

    def evidence_for_chunk(self, *, library_id: str, chunk: dict[str, Any]) -> Any | None: ...

    def resolve_document_render_path(self, *, library_id: str, asset_id: str) -> Path | None: ...
