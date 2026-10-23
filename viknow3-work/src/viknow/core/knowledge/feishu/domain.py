"""Durable domain records for Feishu knowledge synchronization."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeishuTrackingMode(StrEnum):
    SELECTED = "selected"
    SUBTREE = "subtree"
    SPACE = "space"


class FeishuSyncTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class FeishuSyncRunStatus(StrEnum):
    RUNNING = "running"
    INDEXING = "indexing"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"


class FeishuSyncStage(StrEnum):
    QUEUED = "queued"
    DISCOVERING = "discovering"
    DOWNLOADING = "downloading"
    INDEXING = "indexing"
    DELETING = "deleting"
    COMPLETE = "complete"


class FeishuSyncItemStatus(StrEnum):
    WAITING = "waiting"
    UNCHANGED = "unchanged"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class FeishuSyncSource(_StrictModel):
    id: str
    space_id: str
    tenant_host: str | None = None
    credential_ref: str = "configured-environment"
    library_id: str
    node_tokens: list[str] = Field(default_factory=list, max_length=100)
    tracking_mode: FeishuTrackingMode = FeishuTrackingMode.SELECTED
    schedule_enabled: bool = False
    interval_seconds: int = Field(default=10_800, ge=1)
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_tracking_scope(self) -> FeishuSyncSource:
        if self.tracking_mode == FeishuTrackingMode.SPACE:
            if self.node_tokens:
                raise ValueError("space tracking must not persist selected node tokens")
        elif not self.node_tokens:
            raise ValueError("selected and subtree tracking require node tokens")
        return self


class FeishuSyncObject(_StrictModel):
    source_id: str
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    remote_change_hint: str | None = None
    content_digest: str | None = None
    active_upload_id: str | None = None
    active_asset_id: str | None = None
    pending_content_digest: str | None = None
    pending_upload_id: str | None = None
    pending_run_id: str | None = None
    last_synced_at: datetime | None = None
    last_error: str | None = None
    provider_code: int | None = None
    provider_http_status: int | None = None
    created_at: datetime
    updated_at: datetime


class FeishuSyncRunItem(_StrictModel):
    node_token: str
    title: str
    status: FeishuSyncItemStatus
    upload_id: str | None = None
    previous_upload_id: str | None = None
    message: str | None = None
    retryable: bool = False
    provider_code: int | None = None
    provider_http_status: int | None = None
    delete_requested: bool = False


class FeishuSyncRun(_StrictModel):
    id: str
    source_id: str
    trigger: FeishuSyncTrigger
    node_tokens: list[str] = Field(default_factory=list)
    tracking_mode: FeishuTrackingMode = FeishuTrackingMode.SPACE
    requested_schedule_enabled: bool | None = None
    requested_interval_seconds: int | None = Field(default=None, ge=1)
    status: FeishuSyncRunStatus = FeishuSyncRunStatus.RUNNING
    stage: FeishuSyncStage = FeishuSyncStage.DISCOVERING
    items: list[FeishuSyncRunItem] = Field(default_factory=list)
    error_message: str | None = None
    provider_code: int | None = None
    provider_http_status: int | None = None
    retryable: bool = False
    started_at: datetime
    completed_at: datetime | None = None
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.status in {
            FeishuSyncRunStatus.COMPLETED,
            FeishuSyncRunStatus.PARTIAL_FAILED,
            FeishuSyncRunStatus.FAILED,
        }


__all__ = [
    "FeishuSyncItemStatus",
    "FeishuSyncObject",
    "FeishuSyncRun",
    "FeishuSyncRunItem",
    "FeishuSyncRunStatus",
    "FeishuSyncSource",
    "FeishuSyncStage",
    "FeishuSyncTrigger",
    "FeishuTrackingMode",
]
