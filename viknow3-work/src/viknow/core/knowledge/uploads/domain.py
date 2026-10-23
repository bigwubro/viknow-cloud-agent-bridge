"""Domain models for knowledge library file uploads."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class KnowledgeUploadStatus(StrEnum):
    UPLOADING = "uploading"
    NORMALIZING = "normalizing"
    INDEXING = "indexing"
    DONE = "done"
    FAILED = "failed"


class KnowledgeUploadFailureKind(StrEnum):
    UPLOAD = "upload"
    NORMALIZE = "normalize"
    INDEX = "index"


UPLOAD_FAILURE_MESSAGE = "上传失败，请稍后重试"
NORMALIZE_FAILURE_MESSAGE = "视频归一化失败，请稍后重试"
INDEX_FAILURE_MESSAGE = "索引建立失败，请稍后重试"

# Backward-compatible alias used by older call sites/tests.
GENERIC_UPLOAD_FAILURE_MESSAGE = INDEX_FAILURE_MESSAGE

FAILURE_MESSAGES: dict[KnowledgeUploadFailureKind, str] = {
    KnowledgeUploadFailureKind.UPLOAD: UPLOAD_FAILURE_MESSAGE,
    KnowledgeUploadFailureKind.NORMALIZE: NORMALIZE_FAILURE_MESSAGE,
    KnowledgeUploadFailureKind.INDEX: INDEX_FAILURE_MESSAGE,
}


class KnowledgeUploadSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["feishu"]
    url: str | None = None
    node_token: str
    space_id: str


class KnowledgeUploadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    library_id: str
    asset_id: str
    display_name: str
    original_filename: str
    object_key: str
    size_bytes: int
    content_type: str | None = None
    status: KnowledgeUploadStatus
    local_path: str | None = None
    job_id: str | None = None
    failure_kind: KnowledgeUploadFailureKind | None = None
    error_message: str | None = None
    source: KnowledgeUploadSource | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
