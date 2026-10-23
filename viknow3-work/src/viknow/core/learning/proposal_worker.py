"""Worker for durable optimization-analysis tasks."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger

from viknow.core.config import SelfLearningAnalysisConfig
from viknow.core.learning.case_review_service import CaseReviewService


class OptimizationAnalysisWorker:
    def __init__(self, service: CaseReviewService, config: SelfLearningAnalysisConfig) -> None:
        self._service = service
        self._config = config
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._run(), name="viknow-optimization-analysis-worker"
            )

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
        logger.info("[learning] optimization analysis worker started")
        try:
            while not self._stop_event.is_set():
                try:
                    processed = await asyncio.to_thread(
                        self._service.process_pending_analysis_once,
                        limit=self._config.batch_size,
                        lease_seconds=self._config.lease_seconds,
                        max_attempts=self._config.max_attempts,
                    )
                    if processed:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.opt(exception=True).warning(
                        "[learning] optimization analysis worker iteration failed"
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.poll_interval_seconds,
                    )
        finally:
            logger.info("[learning] optimization analysis worker stopped")


def start_optimization_analysis_worker(
    service: CaseReviewService, config: SelfLearningAnalysisConfig
) -> OptimizationAnalysisWorker | None:
    if not config.enabled:
        return None
    worker = OptimizationAnalysisWorker(service, config)
    worker.start()
    return worker
