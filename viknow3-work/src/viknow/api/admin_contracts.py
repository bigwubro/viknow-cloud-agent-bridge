"""Pydantic contracts for Admin API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AdminLoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    token: str


class AdminLogoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True


class PoolGaugeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str
    capacity: int
    in_use: int
    queued: int
    in_use_interactive: int | None = None
    in_use_background: int | None = None
    queue_key: str | None = None
    job_capacity: int | None = None
    tool_in_use: int | None = None
    job_in_use: int | None = None
    queued_tool: int | None = None
    queued_job: int | None = None


class ProfileGaugeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    active_runs: int = 0
    load: float = 0.0
    pools: list[PoolGaugeResponse] = Field(default_factory=list)


class RunStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_count: int = 0
    avg_duration_ms: float | None = None
    p50_duration_ms: float | None = None
    p95_duration_ms: float | None = None


class ToolStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_count: int = 0
    failure_count: int = 0
    by_tool: dict[str, dict[str, int]] = Field(default_factory=dict)


class SchedulerSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    active_runs: int = 0
    load: float | None = None
    scheduler_mode: str = "legacy"
    profiles: list[ProfileGaugeResponse] = Field(default_factory=list)


class SchedulerCurrentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: SchedulerSnapshotResponse | None = None
    runs: RunStatsResponse = Field(default_factory=RunStatsResponse)
    tools: ToolStatsResponse = Field(default_factory=ToolStatsResponse)


class SchedulerHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_ts: datetime = Field(alias="from")
    to_ts: datetime = Field(alias="to")
    snapshots: list[SchedulerSnapshotResponse] = Field(default_factory=list)
    runs: RunStatsResponse = Field(default_factory=RunStatsResponse)
    tools: ToolStatsResponse = Field(default_factory=ToolStatsResponse)


class PoolCapacityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1)
    capacity: int = Field(ge=1)


class SchedulerPoolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pools: list[PoolCapacityItem] = Field(default_factory=list)


class SchedulerPoolsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pools: list[PoolCapacityItem] = Field(min_length=1)


class MultiAgentRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    yaml_enabled: bool
    overridden: bool
    sidecar_profiles: list[str] = Field(default_factory=list)


class MultiAgentRuntimeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class JobStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: str  # pending | queued | running | completed | failed
    task_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    duration_seconds: float | None = None


class JobProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    job_name: str
    workspace: str
    asset_id: str
    status: str  # running | completed | failed | cancelled
    overall_progress: float = 0.0  # 0.0~1.0
    stage_count: int = 0
    completed_stage_count: int = 0
    stages: list[JobStageResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    running_duration_seconds: float | None = None


class IndexJobRowResponse(BaseModel):
    """任务作业列表行：上传记录信息 + job 分阶段进度。"""

    model_config = ConfigDict(extra="forbid")

    upload_id: str
    library_id: str
    asset_id: str
    display_name: str
    original_filename: str
    size_bytes: int
    status: str  # upload 状态：uploading | normalizing | indexing | done | failed
    failure_kind: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    job_id: str | None = None
    job: JobProgressResponse | None = None


class IndexJobsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs: list[IndexJobRowResponse] = Field(default_factory=list)


class IndexJobDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str | None = None
    library_id: str
    asset_id: str
    display_name: str | None = None
    original_filename: str | None = None
    size_bytes: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    job_id: str | None = None
    job: JobProgressResponse | None = None


__all__ = [
    "AdminLoginRequest",
    "AdminLoginResponse",
    "AdminLogoutResponse",
    "IndexJobDetailResponse",
    "IndexJobRowResponse",
    "IndexJobsResponse",
    "JobProgressResponse",
    "JobStageResponse",
    "MultiAgentRuntimeResponse",
    "MultiAgentRuntimeUpdateRequest",
    "PoolCapacityItem",
    "PoolGaugeResponse",
    "ProfileGaugeResponse",
    "RunStatsResponse",
    "SchedulerCurrentResponse",
    "SchedulerHistoryResponse",
    "SchedulerPoolsResponse",
    "SchedulerPoolsUpdateRequest",
    "SchedulerSnapshotResponse",
    "ToolStatsResponse",
]
