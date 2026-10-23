"""Resolve immutable learning inputs before running one isolated SkillOpt experiment."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from viknow.core.learning.case_review import (
    ApprovedCaseSnapshot,
    SkillOptimizationExperiment,
    SkillOptimizationTask,
)


class SkillOptimizationInputStore(Protocol):
    def get_snapshot(self, run_id: str) -> ApprovedCaseSnapshot | None: ...

    def get_oracle_request(self, run_id: str) -> Any | None: ...


class SkillOptimizationExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    result_summary: dict[str, Any]
    acceptance_manifest: dict[str, Any]


AcceptanceRunner = Callable[..., Mapping[str, Any]]


class IsolatedSkillOptExecutor:
    """Bind a durable task to exactly one approved input and run it off-loop."""

    def __init__(
        self,
        *,
        store: SkillOptimizationInputStore,
        acceptance_runner: AcceptanceRunner,
    ) -> None:
        self._store = store
        self._acceptance_runner = acceptance_runner

    async def execute_experiment(
        self, experiment: SkillOptimizationExperiment, *, progress_callback: Callable[[dict], None]
    ) -> SkillOptimizationExecutionResult:
        payload = await asyncio.to_thread(
            self._acceptance_runner.run_experiment,
            experiment,
            progress_callback=progress_callback,
        )
        return SkillOptimizationExecutionResult.model_validate(payload)

    async def execute(self, task: SkillOptimizationTask) -> SkillOptimizationExecutionResult:
        snapshot = None
        accepted_oracle_package = None
        if task.input_source == "gate_a_snapshot":
            snapshot = self._store.get_snapshot(task.source_run_id)
            if snapshot is None:
                raise ValueError("skill optimization requires an approved Gate A snapshot")
        else:
            request = self._store.get_oracle_request(task.source_run_id)
            if request is None or request.id != task.oracle_request_id:
                raise ValueError("skill optimization requires its accepted Oracle request")
            accepted_oracle_package = request.accepted_oracle_package
            if accepted_oracle_package is None:
                raise ValueError("skill optimization requires an accepted Oracle package")

        payload = await asyncio.to_thread(
            self._acceptance_runner,
            task=task,
            approved_snapshot=snapshot,
            accepted_oracle_package=accepted_oracle_package,
        )
        return SkillOptimizationExecutionResult.model_validate(payload)


class SelfLearningSkillOptRunner:
    """Run the existing isolated accepted-Oracle flow for one durable task."""

    def __init__(
        self,
        *,
        snapshot_resolver: Callable[[str], ApprovedCaseSnapshot | None],
        base_config_path: Path,
        skill_root: Path,
        source_profile_path: Path,
        skillopt_package_root: Path,
        skillopt_config_path: Path,
        experiment_root: Path,
        redis_database: int,
        oracle_judge: Any,
        gate_a_judge: Any,
        optimizer_config: Mapping[str, Any],
        optimizer_secret_environment: Mapping[str, str],
        timeout_seconds: float,
        app_config: Any = None,
        legacy_runtime_factory: Callable | None = None,
    ) -> None:
        self._snapshot_resolver = snapshot_resolver
        self._base_config_path = base_config_path
        self._skill_root = skill_root
        self._source_profile_path = source_profile_path
        self._skillopt_package_root = skillopt_package_root
        self._skillopt_config_path = skillopt_config_path
        self._experiment_root = experiment_root
        self._redis_database = redis_database
        self._oracle_judge = oracle_judge
        self._gate_a_judge = gate_a_judge
        self._optimizer_config = dict(optimizer_config)
        self._optimizer_secret_environment = dict(optimizer_secret_environment)
        self._timeout_seconds = timeout_seconds
        self._app_config = app_config
        self._legacy_runtime_factory = legacy_runtime_factory

    def run_experiment(self, experiment: SkillOptimizationExperiment, *, progress_callback):
        from viknow.core.config import EvalJudgeModelConfig, resolve_skill_optimization_model
        from viknow.core.eval.quality_score import build_oracle_requirement_judge
        from viknow.core.eval.skillopt_batch import run_skillopt_batch
        from viknow.core.eval.skillopt_experiment import prepare_skillopt_experiment
        from viknow.core.eval.skillopt_gate_a import build_gate_a_optimization_judge

        if self._app_config is None:
            raise RuntimeError("batch SkillOpt requires the server model directory")
        models = {
            role: resolve_skill_optimization_model(
                self._app_config, getattr(experiment.config, f"{role}_model")
            )
            for role in ("optimizer", "target", "evaluator")
        }
        optimizer_config = {
            "model_backend": "azure_openai",
            "azure_openai_auth_mode": "openai_compatible",
        }
        secrets = {}
        for role in ("optimizer", "target"):
            model = models[role]
            optimizer_config.update(
                {
                    f"{role}_backend": "openai_chat",
                    f"{role}_model": model.require_model(),
                    f"{role}_azure_openai_endpoint": model.resolve_base_url(),
                    f"{role}_azure_openai_auth_mode": "openai_compatible",
                }
            )
            secrets[f"{role.upper()}_AZURE_OPENAI_API_KEY"] = model.require_api_key()
        optimizer_config["azure_openai_endpoint"] = models["optimizer"].resolve_base_url()
        secrets["AZURE_OPENAI_API_KEY"] = models["optimizer"].require_api_key()
        judge_model = models["evaluator"]
        judge_config = EvalJudgeModelConfig(
            model=judge_model.require_model(),
            model_env=None,
            base_url=judge_model.resolve_base_url(),
            base_url_env=None,
            api_key_env=judge_model.api_key_env,
        )
        artifact_id = f"learning-{experiment.id}-attempt-{experiment.attempts}"
        manifest = prepare_skillopt_experiment(
            experiment_id=artifact_id,
            base_config_path=self._base_config_path,
            source_skill_dir=self._skill_root / experiment.target_skill_id,
            source_profile_path=self._source_profile_path,
            output_dir=self._experiment_root / artifact_id,
            redis_database=self._redis_database,
        )
        return run_skillopt_batch(
            experiment=experiment,
            manifest=manifest,
            skillopt_package_root=self._skillopt_package_root,
            output_dir=self._experiment_root / artifact_id / "skillopt-output",
            optimizer_config=optimizer_config,
            optimizer_secret_environment=secrets,
            oracle_judge=build_oracle_requirement_judge(judge_config),
            gate_a_judge=build_gate_a_optimization_judge(judge_config),
            timeout_seconds=self._timeout_seconds,
            progress_callback=progress_callback,
        )

    def __call__(
        self,
        *,
        task: SkillOptimizationTask,
        approved_snapshot: ApprovedCaseSnapshot | None,
        accepted_oracle_package: dict[str, Any] | None,
    ) -> Mapping[str, Any]:
        from viknow.core.eval.candidate_evidence import AcceptedOraclePackage
        from viknow.core.eval.skill_flow import SkillEvalCase
        from viknow.core.eval.skillopt_acceptance import run_skillopt_acceptance
        from viknow.core.eval.skillopt_experiment import prepare_skillopt_experiment

        if self._legacy_runtime_factory is not None:
            (
                self._optimizer_config,
                self._optimizer_secret_environment,
                self._oracle_judge,
                self._gate_a_judge,
            ) = self._legacy_runtime_factory()
        if task.input_source == "gate_a_snapshot":
            if approved_snapshot is None or accepted_oracle_package is not None:
                raise ValueError("Gate A runner received inconsistent input")
            snapshot = approved_snapshot
            accepted = None
        else:
            if approved_snapshot is not None or accepted_oracle_package is None:
                raise ValueError("accepted Oracle runner received inconsistent input")
            snapshot = self._snapshot_resolver(task.source_run_id)
            if snapshot is None:
                raise ValueError("accepted Oracle optimization requires its Gate A snapshot")
            accepted = AcceptedOraclePackage.model_validate(accepted_oracle_package)
            if task.reference_answer is not None:
                accepted = accepted.model_copy(update={"reference_answer": task.reference_answer})
        selected_assets = snapshot.behavior_snapshot.get("knowledge_start_state", {}).get(
            "selected_knowledge_assets", []
        )
        experiment_id = f"learning-{task.id}-attempt-{task.attempts}"
        experiment_dir = self._experiment_root / experiment_id
        manifest = prepare_skillopt_experiment(
            experiment_id=experiment_id,
            base_config_path=self._base_config_path,
            source_skill_dir=self._skill_root / task.target_skill_id,
            source_profile_path=self._source_profile_path,
            output_dir=experiment_dir,
            redis_database=self._redis_database,
        )
        common = {
            "manifest": manifest,
            "skillopt_package_root": self._skillopt_package_root,
            "skillopt_config_path": self._skillopt_config_path,
            "output_dir": experiment_dir / "skillopt-output",
            "optimizer_config": self._optimizer_config,
            "optimizer_secret_environment": self._optimizer_secret_environment,
            "timeout_seconds": self._timeout_seconds,
        }
        if task.input_source == "gate_a_snapshot":
            from viknow.core.eval.skillopt_gate_a import run_skillopt_gate_a_snapshot

            summary = run_skillopt_gate_a_snapshot(
                **common,
                snapshot=snapshot,
                judge=self._gate_a_judge,
                reference_answer=task.reference_answer,
            )
            acceptance_path = Path(summary["viknow_gate_a_acceptance"]["manifest_path"])
        else:
            case = SkillEvalCase(
                case_id=accepted.case_id,
                question=snapshot.question,
                metadata={"selected_knowledge_assets": selected_assets},
            )
            summary = run_skillopt_acceptance(
                **common,
                case=case,
                accepted_oracle=accepted,
                judge=self._oracle_judge,
            )
            acceptance_path = Path(summary["viknow_first_acceptance"]["manifest_path"])
        return {
            "experiment_id": experiment_id,
            "result_summary": summary,
            "acceptance_manifest": json.loads(acceptance_path.read_text(encoding="utf-8")),
        }
