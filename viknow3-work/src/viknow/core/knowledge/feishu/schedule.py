"""Stoppable in-process scheduler for durable Feishu synchronization sources."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from viknow.core.knowledge.feishu.executor import FeishuSyncCoordinator

logger = logging.getLogger(__name__)


class FeishuSyncScheduler:
    def __init__(
        self,
        coordinator: FeishuSyncCoordinator,
        *,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self._coordinator = coordinator
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="feishu-sync-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        await task
        self._task = None

    async def tick(self) -> None:
        await self._coordinator.reconcile_active_runs()
        await self._coordinator.run_due_sources()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Feishu synchronization scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )


__all__ = ["FeishuSyncScheduler"]
