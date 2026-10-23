"""Periodic sampler that persists ResourceScheduler gauges."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger

from viknow.core.scheduler.metrics_domain import (
    MetricsSnapshot,
    ProfileGauge,
    pool_gauge_from_snapshot,
    utc_now,
)
from viknow.core.scheduler.metrics_store import RETENTION_DAYS, SchedulerMetricsStore
from viknow.core.scheduler.resource_scheduler import ResourceScheduler

DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0


class SchedulerMetricsCollector:
    """Sample schedulers from an AgentRuntimeRegistry into Postgres."""

    def __init__(
        self,
        store: SchedulerMetricsStore,
        *,
        runtime_items: Callable[[], Mapping[str, Any] | list[tuple[str, Any]]],
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        self._store = store
        self._runtime_items = runtime_items
        self._interval_seconds = max(1.0, float(interval_seconds))
        self._retention_days = retention_days
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def store(self) -> SchedulerMetricsStore:
        return self._store

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler-metrics-collector")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    def sample_once(self) -> MetricsSnapshot | None:
        profiles = self._collect_profile_gauges()
        if not profiles:
            return None
        snapshot = MetricsSnapshot.from_profile_gauges(ts=utc_now(), profiles=profiles)
        self._store.insert_snapshot(snapshot)
        return snapshot

    def purge_expired(self) -> int:
        return self._store.delete_older_than(days=self._retention_days)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
                self.purge_expired()
            except Exception:
                logger.exception("scheduler metrics collector sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue

    def _collect_profile_gauges(self) -> list[ProfileGauge]:
        items = self._runtime_items()
        pairs = list(items.items()) if isinstance(items, Mapping) else list(items)

        profiles: list[ProfileGauge] = []
        for profile_id, runtime in pairs:
            scheduler = _runtime_scheduler(runtime)
            if scheduler is None or scheduler.scheduler_mode != "unified":
                continue
            resolved_profile_id = str(
                getattr(getattr(runtime, "profile", None), "id", None) or profile_id
            )
            profiles.append(
                ProfileGauge(
                    profile_id=resolved_profile_id,
                    active_runs=scheduler.active_runs,
                    load=scheduler.load,
                    pools=[pool_gauge_from_snapshot(item) for item in scheduler.pool_snapshots()],
                )
            )
        return profiles


def _runtime_scheduler(runtime: Any) -> ResourceScheduler | None:
    scheduler = getattr(runtime, "scheduler", None)
    if isinstance(scheduler, ResourceScheduler):
        return scheduler
    return None


__all__ = ["DEFAULT_SAMPLE_INTERVAL_SECONDS", "SchedulerMetricsCollector"]
