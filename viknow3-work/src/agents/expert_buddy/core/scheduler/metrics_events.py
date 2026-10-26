"""Process-local binder for scheduler metrics event recording."""

from __future__ import annotations

from datetime import datetime

from agents.expert_buddy.core.scheduler.metrics_domain import ToolEvent, ToolEventKind, utc_now
from agents.expert_buddy.core.scheduler.metrics_store import SchedulerMetricsStore

_store: SchedulerMetricsStore | None = None


def bind_scheduler_metrics_store(store: SchedulerMetricsStore | None) -> None:
    """Attach or clear the process-wide metrics store used by runtime/tool hooks."""

    global _store
    _store = store


def get_scheduler_metrics_store() -> SchedulerMetricsStore | None:
    return _store


def record_run_started(
    *,
    run_id: str,
    profile_id: str,
    started_at: datetime | None = None,
) -> None:
    store = _store
    if store is None:
        return
    store.insert_run_started(run_id=run_id, profile_id=profile_id, started_at=started_at)


def record_run_finished(
    *,
    run_id: str,
    profile_id: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    status: str = "completed",
) -> None:
    store = _store
    if store is None:
        return
    store.insert_run_finished(
        run_id=run_id,
        profile_id=profile_id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
    )


def record_tool_timeout(
    *,
    tool_id: str,
    profile_id: str | None = None,
    run_id: str | None = None,
    ts: datetime | None = None,
) -> None:
    store = _store
    if store is None:
        return
    store.insert_tool_event(
        ToolEvent(
            tool_id=tool_id,
            kind=ToolEventKind.TIMEOUT,
            ts=ts or utc_now(),
            profile_id=profile_id,
            run_id=run_id,
        )
    )


def record_tool_failure(
    *,
    tool_id: str,
    profile_id: str | None = None,
    run_id: str | None = None,
    ts: datetime | None = None,
) -> None:
    store = _store
    if store is None:
        return
    store.insert_tool_event(
        ToolEvent(
            tool_id=tool_id,
            kind=ToolEventKind.FAILURE,
            ts=ts or utc_now(),
            profile_id=profile_id,
            run_id=run_id,
        )
    )


__all__ = [
    "bind_scheduler_metrics_store",
    "get_scheduler_metrics_store",
    "record_run_finished",
    "record_run_started",
    "record_tool_failure",
    "record_tool_timeout",
]
