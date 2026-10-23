"""Domain models for the knowledge-base migration package."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MigrationJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ProgressCallback = Callable[[str, float, str | None], Awaitable[None]]


async def report_progress(
    progress: ProgressCallback | None, stage: str, value: float, message: str | None
) -> None:
    if progress is not None:
        await progress(stage, value, message)


class EmbeddingFingerprint(BaseModel):
    """Identifies the vector model so import can reject incompatible packages."""

    embedding_dim: int = 0
    video_embedding_model: str | None = None
    video_embedding_dim: int = 0
    video_segment_table: str | None = None


class ManifestFileEntry(BaseModel):
    """One physical file carried by the package, keyed by its logical path."""

    logical_path: str
    sha256: str
    size_bytes: int


class MigrationManifest(BaseModel):
    """Top-level manifest describing a knowledge package."""

    version: str = "1.0"
    source_workspace: str
    exported_at: float = Field(default_factory=time.time)
    embedding: EmbeddingFingerprint = Field(default_factory=EmbeddingFingerprint)
    files: list[ManifestFileEntry] = Field(default_factory=list)
    table_counts: dict[str, int] = Field(default_factory=dict)


class MigrationJob(BaseModel):
    """In-process async job record for export/import operations."""

    job_id: str
    kind: Literal["export", "import"]
    library_id: str
    status: MigrationJobStatus = MigrationJobStatus.QUEUED
    stage: str | None = None
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
