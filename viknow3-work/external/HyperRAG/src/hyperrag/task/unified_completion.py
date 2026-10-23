"""Fenced HyperRAG terminal notification for scheduler-core 0.1.5.

Core complete() is not idempotent and has no admission ownership check.
WATCH fences its existing Lua, with cached pre-MULTI reads and an explicit
client=transaction Script invocation. No scheduler-core code is copied.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from redis.exceptions import WatchError
from scheduler_core.redis_store import RedisKeys


class _TransactionClient:
    def __init__(self, transaction, snapshots):
        self.transaction = transaction
        self.snapshots = snapshots

    def hgetall(self, key):
        return self.snapshots[key]

    def __getattr__(self, name):
        return getattr(self.transaction, name)


def finish_owned_task(
    store: Any,
    task: Any,
    *,
    started_at: float,
    seq: int,
    hyperrag_task: Any,
    require_expired: bool = False,
    dry_run: bool = False,
    legacy_source_started_at: float | None = None,
) -> str:
    """Notify exactly this admission; never replay an already completed DAG.

    Missing leases are permitted only with the unchanged admission persisted
    in the task HASH. A newly acquired admission changes started_at; WATCH
    also prevents a takeover between validation and EXEC.
    """
    task_key = RedisKeys.task(task.id)
    lease_key = RedisKeys.lease_meta(task.id)
    source_key = f"hyperrag:task:{hyperrag_task.task_id}"
    if not started_at or task.kind.value != "job_task":
        return "unowned"
    try:
        with store.client.pipeline() as tx:
            tx.watch(task_key, lease_key, source_key)
            raw = tx.hgetall(task_key)
            meta = tx.hgetall(lease_key)
            source = json.loads(tx.get(source_key) or "{}")
            if (
                raw.get("kind") != "job_task"
                or raw.get("job_id") != hyperrag_task.job_id
                or task.job_id != hyperrag_task.job_id
                or source.get("task_id") != task.id
                or source.get("job_id") != hyperrag_task.job_id
                or source.get("asset_id") != hyperrag_task.asset_id
                or source.get("status") != hyperrag_task.status
            ):
                return "source_mismatch"
            admission = (source.get("metadata") or {}).get("_unified_admission")
            # Legacy recovery pins the observed old handler start; ordinary
            # workers/recovery never supply this explicitly reviewed override.
            verified_legacy = (
                admission is None
                and legacy_source_started_at is not None
                and source.get("started_at") == legacy_source_started_at
                and legacy_source_started_at >= started_at
                and require_expired
            )
            if admission != {"started_at": started_at, "seq": seq} and not verified_legacy:
                return "source_admission_mismatch"
            if raw.get("status") == "done":
                return "already_done"
            if (
                raw.get("status") != "running"
                or float(raw.get("started_at") or 0) != started_at
                or int(raw.get("seq") or 0) != seq
                or (meta and float(meta.get("acquired_at") or 0) != started_at)
            ):
                return "ownership_changed"
            if require_expired and meta:
                return "active_lease"
            outcome = source.get("status")
            if outcome not in {"completed", "failed", "cancelled"}:
                return "not_terminal"
            if outcome == "completed" and not source.get("completed_at"):
                return "missing_completion"
            if dry_run:
                return "eligible"
            proxy = copy.copy(store)
            # The lease-meta TTL may expire before the shared domain ZSET.
            # Remove only this fenced task's stale members via core complete().
            lease_snapshot = meta or {
                "domains": ",".join(task.domains),
                "kind": "job_task",
            }
            proxy.client = _TransactionClient(tx, {lease_key: lease_snapshot})

            # Script objects bind their original Redis connection by default.
            # Force the original Lua into our WATCH/MULTI transaction instead.
            def complete_script(**kwargs):
                store.scripts.complete(client=tx, **kwargs)
                return 0

            proxy.scripts = SimpleNamespace(complete=complete_script)
            tx.multi()
            if outcome == "completed":
                proxy.complete(task)
            else:
                proxy.mark_failed(task, error=source.get("error_message") or outcome)
                if outcome == "cancelled":
                    tx.hset(task_key, "status", "cancelled")
            tx.execute()
            return outcome
    except WatchError:
        return "ownership_changed"
