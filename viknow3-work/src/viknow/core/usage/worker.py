"""Algorithm-side periodic worker that pushes aggregated usage to the app."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from loguru import logger

from viknow.core.config import LangfuseUsagePushConfig
from viknow.core.usage.aggregate import (
    UsageAggregate,
    aggregate_from_snapshot,
    get_run_usage_aggregate,
    snapshot_has_usage,
)
from viknow.core.usage.pending_store import (
    PendingPush,
    PendingPushStore,
    get_pending_usage_push_store,
    set_usage_push_active,
)
from viknow.core.usage.push_client import UsagePushClient


class UsagePushWorker:
    """Poll pending query runs and index jobs, then push one batch per interval.

    Each callback POST contains every item that became ready since the last
    successful push (plus retries). Items are keyed by ``kind:source_id`` and
    marked pushed only after a 2xx response, so a failed batch is retried
    without dropping or double-marking locally.
    """

    def __init__(
        self,
        config: LangfuseUsagePushConfig,
        *,
        store: PendingPushStore | None = None,
        aggregate_fn: Callable[..., Any] | None = None,
        client: UsagePushClient | None = None,
    ) -> None:
        self._config = config
        self._store = store or get_pending_usage_push_store()
        self._aggregate_fn = aggregate_fn or get_run_usage_aggregate
        token = None
        if config.callback_token_env:
            token = os.getenv(config.callback_token_env)
        self._client = client or UsagePushClient(
            callback_url=config.callback_url or "",
            token=token or None,
            timeout_seconds=config.timeout_seconds,
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        set_usage_push_active(True)
        self._task = asyncio.create_task(
            self._run_loop(),
            name="viknow-usage-push-worker",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        set_usage_push_active(False)
        self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_loop(self) -> None:
        interval = self._config.poll_interval_seconds
        logger.info(
            "[usage] usage push worker started (interval={}s, max_attempts={})",
            interval,
            self._config.max_attempts,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
                try:
                    await self._process_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.opt(exception=True).warning("[usage] push worker iteration failed")
        finally:
            logger.info("[usage] usage push worker stopped")

    async def _process_once(self) -> None:
        ready: list[tuple[PendingPush, UsageAggregate]] = []
        for item in self._store.pending():
            try:
                aggregate = await self._resolve_item(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "[usage] failed to resolve pending {} {}",
                    item.kind,
                    item.source_id,
                )
                self._record_failure(item, "resolve failed")
                continue
            if aggregate is None:
                continue
            ready.append((item, aggregate))
        if not ready:
            return
        await self._push_batch(ready)

    async def _resolve_item(self, item: PendingPush) -> UsageAggregate | None:
        if snapshot_has_usage(item.snapshot):
            return aggregate_from_snapshot(item.snapshot)
        aggregate = await asyncio.to_thread(
            self._aggregate_fn,
            item.langfuse_trace_id,
        )
        if aggregate.status == "not_found":
            self._record_failure(item, aggregate.error or "trace not found")
            return None
        if aggregate.status == "error":
            self._record_failure(item, aggregate.error or "aggregation error")
            return None
        if aggregate.status == "not_ready":
            self._store.record_attempt(
                item.key,
                totals=None,
                error=aggregate.error or "not ready",
            )
            return None
        if aggregate.source != "breakdown":
            self._store.record_attempt(
                item.key,
                totals=aggregate.totals,
                error="",
            )
            current = self._store.get(item.key)
            if current is None or current.stable_count < 1:
                logger.debug(
                    "[usage] {} {} generations not stable yet, deferring",
                    item.kind,
                    item.source_id,
                )
                return None
        return aggregate

    async def _push_batch(self, ready: list[tuple[PendingPush, UsageAggregate]]) -> None:
        payload = {
            "items": [_item_payload(item, aggregate) for item, aggregate in ready],
        }
        accepted = await self._client.push(payload)
        if accepted:
            for item, _aggregate in ready:
                self._store.mark_pushed(item.key)
            logger.info(
                "[usage] pushed usage batch size={} keys={}",
                len(ready),
                [item.key for item, _aggregate in ready],
            )
            return
        for item, _aggregate in ready:
            self._record_failure(item, "application rejected push")

    def _record_failure(self, item: PendingPush, error: str) -> None:
        self._store.record_attempt(item.key, totals=None, error=error)
        refreshed = self._store.get(item.key)
        if refreshed is None:
            return
        if self._config.max_attempts > 0 and refreshed.attempts >= self._config.max_attempts:
            self._store.mark_given_up(item.key, error)
            logger.error(
                "[usage] giving up push for {} {} after {} attempts: {}",
                item.kind,
                item.source_id,
                refreshed.attempts,
                error,
            )
            return
        logger.warning(
            "[usage] push not accepted for {} {} (attempt {}): {}",
            item.kind,
            item.source_id,
            refreshed.attempts,
            error,
        )


def _item_payload(item: PendingPush, aggregate: UsageAggregate) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": item.source_id,
        "idempotency_key": item.key,
        "langfuse_trace_id": item.langfuse_trace_id,
        "totals": aggregate.totals,
        "by_model": aggregate.by_model,
        "source": aggregate.source,
    }


def start_usage_push_worker(config: LangfuseUsagePushConfig) -> UsagePushWorker | None:
    """Start the worker when configured with a callback URL; otherwise no-op."""
    if not config.enabled:
        logger.info("[usage] usage push worker disabled by config")
        set_usage_push_active(False)
        return None
    if not config.callback_url:
        logger.info("[usage] usage push worker skipped: callback_url not configured")
        set_usage_push_active(False)
        return None
    worker = UsagePushWorker(config)
    worker.start()
    return worker
