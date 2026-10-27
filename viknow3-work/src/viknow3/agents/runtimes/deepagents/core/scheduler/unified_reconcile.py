"""Reconcile unified scheduler Redis queues after crashed agent sessions.

Queued ``AGENT_TOOL`` rows older than ``QUEUE_ORPHAN_AFTER_SECONDS`` are
cancelled. Index ``JOB_TASK`` rows are left for HyperRAG.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from scheduler_core.models import TaskKind, TaskStatus
from scheduler_core.scheduler import task_from_hash
from scheduler_core.store import SchedulerStore, task_ts

from viknow3.agents.runtimes.deepagents.core.config import AppConfig

logger = logging.getLogger(__name__)

QUEUE_ORPHAN_AFTER_SECONDS = 300.0
ORPHAN_RECONCILE_INTERVAL_SECONDS = 30.0

QueueOrphanAction = Literal["keep", "cancel", "pop"]


@dataclass(frozen=True)
class QueueReconcileStats:
    cancelled: int = 0
    popped: int = 0

    @property
    def removed(self) -> int:
        return self.cancelled + self.popped


@dataclass(frozen=True)
class UnifiedReconcileStats:
    orphans: QueueReconcileStats = QueueReconcileStats()
    stale_runs: int = 0

    @property
    def removed(self) -> int:
        return self.orphans.removed + self.stale_runs


def created_at_is_stale(
    created_at: float,
    *,
    now: float,
    stale_after: float = QUEUE_ORPHAN_AFTER_SECONDS,
) -> bool:
    if created_at <= 0:
        return True
    return now - created_at >= stale_after


def classify_queued_orphan(
    raw: dict[str, str] | None,
    *,
    now: float,
    stale_after: float = QUEUE_ORPHAN_AFTER_SECONDS,
) -> QueueOrphanAction:
    if not raw:
        return "pop"
    status = raw.get("status") or ""
    if status in {
        TaskStatus.DONE.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return "pop"
    if (raw.get("kind") or "") != TaskKind.AGENT_TOOL.value:
        return "keep"
    created = float(raw.get("created_at") or 0)
    if created_at_is_stale(created, now=now, stale_after=stale_after):
        return "cancel"
    return "keep"


def _domain_names(store: SchedulerStore) -> list[str]:
    return list(store.config.domains)


def _pop_from_domains(store: SchedulerStore, task_id: str, domains: list[str]) -> None:
    for domain in domains or _domain_names(store):
        store.pop_queue(domain, task_id)


def reconcile_orphan_queue_tasks(
    store: SchedulerStore,
    *,
    stale_after: float = QUEUE_ORPHAN_AFTER_SECONDS,
    now: float | None = None,
) -> QueueReconcileStats:
    """Remove queued AGENT_TOOL rows older than ``stale_after`` seconds."""

    now = task_ts() if now is None else now
    seen: set[str] = set()
    cancelled = 0
    popped = 0
    for domain in _domain_names(store):
        for task_id in store.queued_ids(domain):
            if task_id in seen:
                continue
            seen.add(task_id)
            raw = store.get_task(task_id)
            action = classify_queued_orphan(raw, now=now, stale_after=stale_after)
            if action == "keep":
                continue
            if action == "pop" or raw is None:
                domains = [d for d in ((raw or {}).get("domains") or "").split(",") if d]
                _pop_from_domains(store, task_id, domains or _domain_names(store))
                popped += 1
                continue
            task = task_from_hash(task_id, raw)
            if store.cancel(task):
                cancelled += 1
                continue
            _pop_from_domains(store, task_id, list(task.domains) or _domain_names(store))
            popped += 1
    return QueueReconcileStats(cancelled=cancelled, popped=popped)


def reconcile_unified_scheduler_state(
    config: AppConfig,
    *,
    stale_after: float = QUEUE_ORPHAN_AFTER_SECONDS,
) -> UnifiedReconcileStats:
    unified = config.scheduler.unified
    if not unified.enabled:
        return UnifiedReconcileStats()

    scheduler_config = unified.to_scheduler_config()
    store = SchedulerStore.from_url(scheduler_config.redis_url, scheduler_config)
    orphans = reconcile_orphan_queue_tasks(store, stale_after=stale_after)
    if orphans.removed:
        logger.warning(
            "Reconciled %s orphan agent queue task(s) (cancelled=%s popped=%s)",
            orphans.removed,
            orphans.cancelled,
            orphans.popped,
        )
    stale_runs = store.reconcile_stale_runs()
    if stale_runs:
        logger.warning(
            "Reconciled %s stale unified scheduler run(s) from Redis",
            stale_runs,
        )
    return UnifiedReconcileStats(orphans=orphans, stale_runs=stale_runs)


def reconcile_unified_scheduler_runs(
    config: AppConfig,
    *,
    stale_after_seconds: float | None = None,
) -> int:
    """Drop stale runs after reclaiming queued AGENT_TOOL rows older than 300s."""

    stale_after = QUEUE_ORPHAN_AFTER_SECONDS if stale_after_seconds is None else stale_after_seconds
    return reconcile_unified_scheduler_state(config, stale_after=stale_after).removed


async def run_orphan_reconcile_loop(
    config: AppConfig,
    *,
    stop: asyncio.Event,
    interval_seconds: float = ORPHAN_RECONCILE_INTERVAL_SECONDS,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            reconcile_unified_scheduler_state(config)
        except Exception:
            logger.exception("periodic unified queue orphan reconcile failed")


__all__ = [
    "ORPHAN_RECONCILE_INTERVAL_SECONDS",
    "QUEUE_ORPHAN_AFTER_SECONDS",
    "QueueReconcileStats",
    "UnifiedReconcileStats",
    "classify_queued_orphan",
    "created_at_is_stale",
    "reconcile_orphan_queue_tasks",
    "reconcile_unified_scheduler_runs",
    "reconcile_unified_scheduler_state",
    "run_orphan_reconcile_loop",
]
