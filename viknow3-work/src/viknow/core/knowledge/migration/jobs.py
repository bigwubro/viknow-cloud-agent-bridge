"""In-process async job management for knowledge migration."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from viknow.core.knowledge.migration.models import (
    MigrationJob,
    MigrationJobStatus,
    ProgressCallback,
)


class MigrationJobManager:
    """Process-local registry of export/import jobs.

    Jobs run on the same event loop as ``asyncio.create_task``; status and
    progress are updated in place so ``get()`` always reflects the live state.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, MigrationJob] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.time()

    def _snapshot(self, job: MigrationJob) -> MigrationJob:
        return job.model_copy(deep=True)

    async def submit(
        self,
        *,
        kind: str,
        library_id: str,
        runner: Callable[[ProgressCallback], Awaitable[dict[str, Any]]],
    ) -> str:
        job_id = str(uuid.uuid4())
        job = MigrationJob(
            job_id=job_id,
            kind=kind,
            library_id=library_id,
            status=MigrationJobStatus.QUEUED,
        )
        async with self._lock:
            self._jobs[job_id] = job

        async def track(progress: ProgressCallback | None) -> None:
            await self._update(
                job_id,
                status=MigrationJobStatus.RUNNING,
                stage="start",
                progress=0.0,
                message="任务已启动",
            )
            # Yield so the HTTP accept response can flush before heavy sync work.
            await asyncio.sleep(0)
            try:
                result = await runner(progress)
                await self._update(
                    job_id,
                    status=MigrationJobStatus.SUCCEEDED,
                    progress=1.0,
                    message="任务完成",
                    result=result,
                )
            except asyncio.CancelledError:
                await self._update(
                    job_id,
                    status=MigrationJobStatus.FAILED,
                    error="任务已取消",
                )
                raise
            except Exception as exc:
                await self._update(
                    job_id,
                    status=MigrationJobStatus.FAILED,
                    error=str(exc) or type(exc).__name__,
                )

        async def _progress_reporter(stage: str, value: float, message: str | None) -> None:
            await self._update(job_id, stage=stage, progress=value, message=message)

        asyncio.get_running_loop().create_task(track(_progress_reporter))
        return job_id

    async def _update(
        self,
        job_id: str,
        *,
        status: MigrationJobStatus | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            updates: dict[str, Any] = {"updated_at": self._now()}
            if status is not None:
                updates["status"] = status
            if stage is not None:
                updates["stage"] = stage
            if progress is not None:
                updates["progress"] = progress
            if message is not None:
                updates["message"] = message
            if error is not None:
                updates["error"] = error
            if result is not None:
                updates["result"] = result
            self._jobs[job_id] = job.model_copy(update=updates)

    async def get(self, job_id: str) -> MigrationJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            return self._snapshot(job) if job is not None else None

    async def list_recent(self, limit: int = 50) -> list[MigrationJob]:
        async with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [self._snapshot(job) for job in jobs[:limit]]
