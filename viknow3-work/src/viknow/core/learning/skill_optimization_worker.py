"""Durable worker boundary for isolated Skill optimization experiments."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from loguru import logger

from viknow.core.learning.case_review import SkillOptimizationTask
from viknow.core.learning.case_review_store import LeaseOwnershipLost


class SkillOptimizationExecutor(Protocol):
    async def execute(self, task: SkillOptimizationTask) -> Any: ...


class SkillOptimizationWorker:
    def __init__(
        self, *, store: Any, executor: SkillOptimizationExecutor, config: Any = None
    ) -> None:
        self._store = store
        self._executor = executor
        self._config = config
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._config is None:
            raise ValueError("continuous SkillOpt worker requires configuration")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="viknow-skillopt-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("[learning] SkillOpt worker started")
        try:
            while not self._stop.is_set():
                processed = await self.process_once(
                    limit=self._config.batch_size,
                    lease_seconds=self._config.lease_seconds,
                    max_attempts=self._config.max_attempts,
                )
                if not processed:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self._config.poll_interval_seconds
                        )
        finally:
            logger.info("[learning] SkillOpt worker stopped")

    async def process_once(
        self,
        *,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> int:
        processed = 0
        experiments = await asyncio.to_thread(
            self._store.claim_skill_optimization_experiments,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        for experiment in experiments:
            processed += await self._process_experiment(experiment, lease_seconds, max_attempts)
        if experiments:
            return processed
        tasks = await asyncio.to_thread(
            self._store.claim_skill_optimization_tasks,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        for task in tasks:
            try:
                result = await self._executor.execute(task)
                await asyncio.to_thread(
                    self._store.complete_skill_optimization_task,
                    task.id,
                    attempt=task.attempts,
                    experiment_id=result.experiment_id,
                    result_summary=result.result_summary,
                    acceptance_manifest=result.acceptance_manifest,
                )
                processed += 1
                logger.bind(task_id=task.id, experiment_id=result.experiment_id).info(
                    "[learning] Skill optimization awaiting acceptance"
                )
            except LeaseOwnershipLost:
                logger.bind(task_id=task.id, attempt=task.attempts).info(
                    "[learning] stale SkillOpt result discarded"
                )
            except Exception as exc:
                try:
                    await asyncio.to_thread(
                        self._store.fail_skill_optimization_task,
                        task.id,
                        attempt=task.attempts,
                        error_code=type(exc).__name__,
                        max_attempts=max_attempts,
                    )
                except LeaseOwnershipLost:
                    logger.bind(task_id=task.id, attempt=task.attempts).info(
                        "[learning] stale SkillOpt failure discarded"
                    )
                    continue
                logger.bind(task_id=task.id, error_code=type(exc).__name__).opt(
                    exception=exc
                ).warning("[learning] Skill optimization failed")
        return processed

    async def _process_experiment(self, experiment, lease_seconds, max_attempts):
        def update(**changes):
            return self._store.update_skill_optimization_experiment(
                experiment.id, attempt=experiment.attempts, **changes
            )

        async def renew():
            while True:
                await asyncio.sleep(lease_seconds / 3)
                await asyncio.to_thread(
                    update, lease_until=datetime.now(UTC) + timedelta(seconds=lease_seconds)
                )

        heartbeat = asyncio.create_task(renew())
        execution = asyncio.create_task(
            self._executor.execute_experiment(
                experiment,
                progress_callback=lambda progress: update(progress=progress),
            )
        )
        try:
            try:
                result = await asyncio.shield(execution)
            except asyncio.CancelledError:
                # Cancelling to_thread cannot stop its trainer. Keep ownership until
                # it finishes so another process cannot claim a still-running batch.
                result = await execution
            await asyncio.to_thread(
                update,
                status="pending_acceptance",
                lease_until=None,
                artifact_id=result.experiment_id,
                result_summary=result.result_summary,
                acceptance_manifest=result.acceptance_manifest,
                progress={"stage": "pending_acceptance"},
            )
            return 1
        except LeaseOwnershipLost:
            logger.bind(experiment_id=experiment.id, attempt=experiment.attempts).info(
                "[learning] stale batch SkillOpt result discarded"
            )
            return 0
        except Exception as exc:
            summary = str(exc)
            for name, value in os.environ.items():
                if len(value) >= 4 and any(
                    word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")
                ):
                    summary = summary.replace(value, "[redacted]")
            summary = re.sub(r"https?://\S+", "[endpoint]", summary)
            try:
                await asyncio.to_thread(
                    update,
                    status="failed" if experiment.attempts >= max_attempts else "queued",
                    lease_until=None,
                    error_code=type(exc).__name__,
                    error_summary=summary[:2000],
                    progress={"stage": "failed"},
                )
            except LeaseOwnershipLost:
                logger.bind(experiment_id=experiment.id, attempt=experiment.attempts).info(
                    "[learning] stale batch SkillOpt failure discarded"
                )
                return 0
            logger.bind(experiment_id=experiment.id, error_code=type(exc).__name__).warning(
                "[learning] Batch SkillOpt execution failed"
            )
            return 0
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, LeaseOwnershipLost):
                await heartbeat


def start_skill_optimization_worker(
    *,
    review_service: Any,
    store: Any,
    config: Any,
    app_config: Any = None,
    executor: SkillOptimizationExecutor | None = None,
) -> SkillOptimizationWorker | None:
    if not config.enabled:
        return None
    if executor is None:
        executor = _build_executor(
            review_service=review_service,
            store=store,
            config=config,
            app_config=app_config,
        )
    worker = SkillOptimizationWorker(store=store, executor=executor, config=config)
    worker.start()
    return worker


def _build_executor(*, review_service: Any, store: Any, config: Any, app_config: Any) -> Any:
    from viknow.core.learning.skill_optimization_executor import (
        IsolatedSkillOptExecutor,
        SelfLearningSkillOptRunner,
    )

    if app_config is None:
        raise RuntimeError("enabled SkillOpt worker requires the application configuration")
    runner = SelfLearningSkillOptRunner(
        snapshot_resolver=review_service.snapshot,
        base_config_path=config.base_config_path,
        skill_root=config.source_skill_root,
        source_profile_path=config.source_profile_path,
        skillopt_package_root=config.skillopt_package_root,
        skillopt_config_path=config.skillopt_config_path,
        experiment_root=config.experiment_root,
        redis_database=config.redis_database,
        oracle_judge=None,
        gate_a_judge=None,
        optimizer_config={},
        optimizer_secret_environment={},
        timeout_seconds=config.timeout_seconds,
        app_config=app_config,
        legacy_runtime_factory=lambda: _legacy_runtime(config, app_config),
    )
    return IsolatedSkillOptExecutor(store=store, acceptance_runner=runner)


def _legacy_runtime(config, app_config):
    from viknow.core.eval.quality_score import build_oracle_requirement_judge
    from viknow.core.eval.skillopt_gate_a import build_gate_a_optimization_judge

    model = _required_environment(config.optimizer_model_env)
    base_url = _required_environment(config.optimizer_base_url_env)
    api_key = _required_environment(config.optimizer_api_key_env)
    optimizer_config = {
        "model_backend": "azure_openai",
        "optimizer_backend": "openai_chat",
        "target_backend": "openai_chat",
        "optimizer_model": model,
        "target_model": model,
        "azure_openai_endpoint": base_url,
        "azure_openai_auth_mode": "openai_compatible",
        "optimizer_azure_openai_endpoint": base_url,
        "optimizer_azure_openai_auth_mode": "openai_compatible",
        "target_azure_openai_endpoint": base_url,
        "target_azure_openai_auth_mode": "openai_compatible",
    }
    secrets = {
        "AZURE_OPENAI_API_KEY": api_key,
        "OPTIMIZER_AZURE_OPENAI_API_KEY": api_key,
        "TARGET_AZURE_OPENAI_API_KEY": api_key,
    }
    return (
        optimizer_config,
        secrets,
        build_oracle_requirement_judge(app_config.eval.skill_flow.oracle_judge_model),
        build_gate_a_optimization_judge(app_config.eval.skill_flow.analysis_model),
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required SkillOpt environment variable is unavailable: {name}")
    return value
