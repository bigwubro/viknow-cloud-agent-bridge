"""Domain models for ResourceScheduler metrics snapshots and events."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class ToolEventKind(StrEnum):
    TIMEOUT = "timeout"
    FAILURE = "failure"


class PoolGauge(BaseModel):
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


class ProfileGauge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    active_runs: int = 0
    load: float = 0.0
    pools: list[PoolGauge] = Field(default_factory=list)


class MetricsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    profiles: list[ProfileGauge] = Field(default_factory=list)
    active_runs: int = 0
    load: float | None = None
    scheduler_mode: str = "legacy"

    @classmethod
    def from_profile_gauges(cls, *, ts: datetime, profiles: list[ProfileGauge]) -> MetricsSnapshot:
        active_runs = sum(item.active_runs for item in profiles)
        load = max((item.load for item in profiles), default=None)
        return cls(
            ts=ts,
            profiles=profiles,
            active_runs=active_runs,
            load=load,
        )


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    profile_id: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    status: str | None = None


class ToolEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    kind: ToolEventKind
    ts: datetime
    profile_id: str | None = None
    run_id: str | None = None


class RunDurationStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_count: int = 0
    avg_duration_ms: float | None = None
    p50_duration_ms: float | None = None
    p95_duration_ms: float | None = None


class ToolKindStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_count: int = 0
    failure_count: int = 0
    by_tool: dict[str, dict[str, int]] = Field(default_factory=dict)


class SchedulerWindowStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: RunDurationStats = Field(default_factory=RunDurationStats)
    tools: ToolKindStats = Field(default_factory=ToolKindStats)


def pool_gauge_from_snapshot(item: dict[str, Any]) -> PoolGauge:
    return PoolGauge(
        resource=str(item.get("resource") or ""),
        capacity=int(item.get("capacity") or 0),
        in_use=int(item.get("in_use") or 0),
        queued=int(item.get("queued") or 0),
        in_use_interactive=_optional_int(item.get("in_use_interactive")),
        in_use_background=_optional_int(item.get("in_use_background")),
        queue_key=item.get("queue_key"),
        job_capacity=_optional_int(item.get("job_capacity")),
        tool_in_use=_optional_int(item.get("tool_in_use")),
        job_in_use=_optional_int(item.get("job_in_use")),
        queued_tool=_optional_int(item.get("queued_tool")),
        queued_job=_optional_int(item.get("queued_job")),
    )


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Return the q-quantile (0..1) for a pre-sorted non-empty numeric series."""

    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    q = max(0.0, min(1.0, q))
    rank = q * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


def aggregate_run_durations(duration_ms_values: list[int]) -> RunDurationStats:
    if not duration_ms_values:
        return RunDurationStats()
    ordered = sorted(float(value) for value in duration_ms_values)
    total = sum(ordered)
    return RunDurationStats(
        run_count=len(ordered),
        avg_duration_ms=total / len(ordered),
        p50_duration_ms=percentile(ordered, 0.5),
        p95_duration_ms=percentile(ordered, 0.95),
    )


def aggregate_tool_events(events: list[ToolEvent]) -> ToolKindStats:
    timeout_count = 0
    failure_count = 0
    by_tool: dict[str, dict[str, int]] = {}
    for event in events:
        bucket = by_tool.setdefault(event.tool_id, {"timeout": 0, "failure": 0})
        if event.kind is ToolEventKind.TIMEOUT:
            timeout_count += 1
            bucket["timeout"] += 1
        else:
            failure_count += 1
            bucket["failure"] += 1
    return ToolKindStats(
        timeout_count=timeout_count,
        failure_count=failure_count,
        by_tool=by_tool,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


__all__ = [
    "MetricsSnapshot",
    "PoolGauge",
    "ProfileGauge",
    "RunDurationStats",
    "RunEvent",
    "SchedulerWindowStats",
    "ToolEvent",
    "ToolEventKind",
    "ToolKindStats",
    "aggregate_run_durations",
    "aggregate_tool_events",
    "percentile",
    "pool_gauge_from_snapshot",
    "utc_now",
]
