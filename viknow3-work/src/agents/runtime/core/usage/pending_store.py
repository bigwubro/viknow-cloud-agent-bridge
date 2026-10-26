"""Pending usage-push registry for completed query runs and index jobs.

In-memory per-process store. Query runs and index jobs register at completion;
the worker batches every pending ready item into one callback per poll interval.
A persistent store can replace this later without changing the worker contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

STATUS_PENDING = "pending"
STATUS_PUSHED = "pushed"
STATUS_GIVEN_UP = "given_up"

KIND_QUERY = "query"
KIND_JOB = "job"

_EMPTY_TOTALS: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def empty_usage_snapshot() -> dict[str, Any]:
    return {"totals": dict(_EMPTY_TOTALS), "by_model": []}


def pending_item_key(kind: str, source_id: str) -> str:
    return f"{kind}:{source_id}"


@dataclass
class PendingPush:
    """One completed query run or index job waiting to be pushed."""

    kind: str
    source_id: str
    langfuse_trace_id: str
    snapshot: dict[str, Any] = field(default_factory=empty_usage_snapshot)
    status: str = STATUS_PENDING
    attempts: int = 0
    last_error: str = ""
    last_totals: dict[str, int] | None = None
    stable_count: int = 0

    @property
    def key(self) -> str:
        return pending_item_key(self.kind, self.source_id)


class PendingPushStore:
    """Thread-safe registry keyed by ``kind:source_id``."""

    def __init__(self) -> None:
        self._items: dict[str, PendingPush] = {}
        self._lock = threading.Lock()

    def register(
        self,
        *,
        kind: str,
        source_id: str,
        langfuse_trace_id: str,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        key = pending_item_key(kind, source_id)
        payload = dict(snapshot) if snapshot else empty_usage_snapshot()
        with self._lock:
            existing = self._items.get(key)
            if existing is not None and existing.status == STATUS_PUSHED:
                return
            if existing is None:
                self._items[key] = PendingPush(
                    kind=kind,
                    source_id=source_id,
                    langfuse_trace_id=langfuse_trace_id,
                    snapshot=payload,
                )
                return
            existing.langfuse_trace_id = langfuse_trace_id
            existing.snapshot = payload
            existing.status = STATUS_PENDING

    def pending(self) -> list[PendingPush]:
        with self._lock:
            return [item for item in self._items.values() if item.status == STATUS_PENDING]

    def get(self, key: str) -> PendingPush | None:
        with self._lock:
            return self._items.get(key)

    def record_attempt(
        self,
        key: str,
        *,
        totals: dict[str, int] | None,
        error: str,
    ) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item.attempts += 1
            item.last_error = error
            if totals is not None:
                if item.last_totals == totals:
                    item.stable_count += 1
                else:
                    item.stable_count = 0
                item.last_totals = dict(totals)

    def mark_pushed(self, key: str) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item.status = STATUS_PUSHED
            item.last_error = ""

    def mark_given_up(self, key: str, error: str) -> None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return
            item.status = STATUS_GIVEN_UP
            item.last_error = error

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_store: PendingPushStore | None = None
_store_lock = threading.Lock()
_push_active = False
_push_active_lock = threading.Lock()


def get_pending_usage_push_store() -> PendingPushStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PendingPushStore()
    return _store


def set_usage_push_active(active: bool) -> None:
    global _push_active
    with _push_active_lock:
        _push_active = active


def is_usage_push_active() -> bool:
    return _push_active


def register_pending_usage_push(
    *,
    kind: str,
    source_id: str,
    langfuse_trace_id: str,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Register a completed query run or index job for the next batch push."""
    if not is_usage_push_active():
        return
    if not kind or not source_id or not langfuse_trace_id:
        logger.debug("[usage] skip pending registration without kind/id/trace")
        return
    store = get_pending_usage_push_store()
    try:
        store.register(
            kind=kind,
            source_id=source_id,
            langfuse_trace_id=langfuse_trace_id,
            snapshot=snapshot,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "[usage] failed to register pending push for {} {}",
            kind,
            source_id,
        )
