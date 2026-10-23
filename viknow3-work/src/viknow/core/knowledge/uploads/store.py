"""Persistence protocol for knowledge upload records."""

from __future__ import annotations

from typing import Protocol

from viknow.core.knowledge.uploads.domain import KnowledgeUploadRecord


class KnowledgeUploadStore(Protocol):
    def add(self, record: KnowledgeUploadRecord) -> KnowledgeUploadRecord: ...

    def get(
        self, upload_id: str, *, include_deleted: bool = False
    ) -> KnowledgeUploadRecord | None: ...

    def list_for_library(
        self,
        *,
        library_id: str,
        include_deleted: bool = False,
    ) -> list[KnowledgeUploadRecord]: ...

    def update(self, record: KnowledgeUploadRecord) -> KnowledgeUploadRecord: ...

    def soft_delete(self, upload_id: str) -> bool: ...

    def list_display_names(self, *, library_id: str) -> set[str]: ...
