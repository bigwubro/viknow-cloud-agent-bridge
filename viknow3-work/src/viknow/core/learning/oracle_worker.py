"""Execution boundary for durable Evidence Oracle requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from loguru import logger
from pydantic import BaseModel, ConfigDict

from viknow.core.config import SelfLearningOracleConfig
from viknow.core.eval.candidate_evidence import CandidateEvidencePackage
from viknow.core.learning.case_review import ApprovedCaseSnapshot, OracleRequest
from viknow.core.learning.case_review_store import LeaseOwnershipLost
from viknow.core.learning.oracle_execution import (
    AssetRevisionResolver,
    build_evidence_collection_task,
)


class OracleEvalService(Protocol):
    async def eval_run(self, **kwargs: Any) -> Any: ...


class OracleExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    oracle_run_id: str
    candidate_evidence: CandidateEvidencePackage


class OracleRequestExecutor:
    def __init__(
        self,
        *,
        eval_service: OracleEvalService,
        revision_resolver: AssetRevisionResolver,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._eval_service = eval_service
        self._revision_resolver = revision_resolver
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        *,
        request: OracleRequest,
        snapshot: ApprovedCaseSnapshot,
        on_run_created: Callable[[str], None],
    ) -> OracleExecutionResult:
        task = build_evidence_collection_task(
            request=request,
            snapshot=snapshot,
            revision_resolver=self._revision_resolver,
        )
        trace = await self._eval_service.eval_run(
            query=snapshot.question,
            profile_id="evidence-oracle",
            evidence_collection_task=task,
            library_ids=[task.scope.library_id],
            timeout_seconds=self._timeout_seconds,
            on_run_created=on_run_created,
        )
        packages = trace.metadata.candidate_evidence_packages
        if len(packages) != 1:
            raise ValueError("Oracle run must produce exactly one candidate evidence package")
        package = CandidateEvidencePackage.model_validate(packages[0])
        if _approved_task_identity(package.task) != _approved_task_identity(task):
            raise ValueError("Oracle candidate package task differs from approved task")
        return OracleExecutionResult(
            oracle_run_id=trace.run_id,
            candidate_evidence=package,
        )


def _approved_task_identity(task: Any) -> dict[str, Any]:
    """Compare only Gate-A-owned fields; research fields are enriched during the run."""

    return task.model_dump(
        mode="json",
        exclude={"research_seeds", "research_plan", "research_frontier"},
    )


class DurableOracleWorker:
    def __init__(
        self, *, review_service: Any, store: Any, executor: Any, config: SelfLearningOracleConfig
    ) -> None:
        self._review_service = review_service
        self._store = store
        self._executor = executor
        self._config = config
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="viknow-evidence-oracle-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        self._stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        logger.info("[learning] Evidence Oracle worker started")
        while not self._stop.is_set():
            requests = await asyncio.to_thread(
                self._store.claim_oracle_requests,
                limit=self._config.batch_size,
                lease_seconds=self._config.lease_seconds,
            )
            for request in requests:
                try:
                    snapshot = await asyncio.to_thread(
                        self._review_service.snapshot, request.source_run_id
                    )

                    def mark_run_started(
                        run_id: str,
                        request_id: str = request.id,
                        attempt: int = request.attempts,
                    ) -> None:
                        self._store.mark_oracle_request_run_started(
                            request_id, attempt=attempt, oracle_run_id=run_id
                        )

                    result = await self._executor.execute(
                        request=request,
                        snapshot=snapshot,
                        on_run_created=mark_run_started,
                    )
                    task = result.candidate_evidence.task
                    await asyncio.to_thread(
                        self._store.complete_oracle_request,
                        request.id,
                        attempt=request.attempts,
                        evidence_collection_task=task.model_dump(mode="json"),
                        oracle_run_id=result.oracle_run_id,
                        candidate_evidence_package=result.candidate_evidence.model_dump(
                            mode="json"
                        ),
                    )
                    logger.bind(request_id=request.id, oracle_run_id=result.oracle_run_id).info(
                        "[learning] Evidence Oracle request awaiting Gate B"
                    )
                except asyncio.CancelledError:
                    raise
                except LeaseOwnershipLost:
                    logger.bind(request_id=request.id, attempt=request.attempts).info(
                        "[learning] stale Evidence Oracle result discarded"
                    )
                except Exception as exc:
                    try:
                        await asyncio.to_thread(
                            self._store.fail_oracle_request,
                            request.id,
                            attempt=request.attempts,
                            error_code=type(exc).__name__,
                            max_attempts=self._config.max_attempts,
                        )
                    except LeaseOwnershipLost:
                        logger.bind(request_id=request.id, attempt=request.attempts).info(
                            "[learning] stale Evidence Oracle failure discarded"
                        )
                        continue
                    logger.bind(request_id=request.id, error_code=type(exc).__name__).warning(
                        "[learning] Evidence Oracle request failed"
                    )
            if not requests:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._config.poll_interval_seconds
                    )


def start_oracle_worker(
    *,
    review_service: Any,
    store: Any,
    eval_service: OracleEvalService,
    revision_resolver: AssetRevisionResolver,
    config: SelfLearningOracleConfig,
) -> DurableOracleWorker | None:
    if not config.enabled:
        return None
    worker = DurableOracleWorker(
        review_service=review_service,
        store=store,
        executor=OracleRequestExecutor(
            eval_service=eval_service,
            revision_resolver=revision_resolver,
            timeout_seconds=config.timeout_seconds,
        ),
        config=config,
    )
    worker.start()
    return worker
