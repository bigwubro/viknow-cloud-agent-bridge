"""In-process worker that drains the durable Learning Projection outbox."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger

from viknow.core.config import LearningProjectionConfig
from viknow.core.learning.service import LearningProjectionService


class LearningProjectionWorker:
    """Poll a durable outbox; failures remain retryable according to configuration."""

    def __init__(
        self,
        service: LearningProjectionService,
        config: LearningProjectionConfig,
    ) -> None:
        self._service = service
        self._config = config
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="viknow-learning-projection-worker")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        logger.info("[learning] projection worker started")
        try:
            while not self._stop_event.is_set():
                try:
                    processed = await asyncio.to_thread(
                        self._service.process_pending_once,
                        limit=self._config.batch_size,
                    )
                    if processed:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.opt(exception=True).warning(
                        "[learning] projection worker iteration failed"
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._config.poll_interval_seconds
                    )
        finally:
            logger.info("[learning] projection worker stopped")


def start_learning_projection_worker(
    service: LearningProjectionService | None,
    config: LearningProjectionConfig,
) -> LearningProjectionWorker | None:
    if service is None or not config.enabled:
        return None
    worker = LearningProjectionWorker(service, config)
    worker.start()
    return worker
