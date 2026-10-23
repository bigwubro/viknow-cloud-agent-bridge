"""Project-owned boundaries for the first accepted-Oracle SkillOpt run."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.accepted_scoring import score_accepted_trace
from viknow.core.eval.candidate_evidence import (
    AcceptedOraclePackage,
    accepted_oracle_as_evidence_package,
)
from viknow.core.eval.quality_score import OracleRequirementJudge, QualityScorecard
from viknow.core.eval.skill_flow import SkillEvalCase
from viknow.core.eval.skillopt_experiment import (
    SkillOptExperimentManifest,
    validate_skillopt_experiment_copy,
)
from viknow.core.eval.skillopt_projection import (
    build_skillopt_evaluation_artifact,
    skillopt_evaluator_result,
)
from viknow.core.eval.version_compare import (
    AcceptedComparisonProvenance,
    SkillFlowVersionComparison,
    compare_accepted_quality_run_payloads,
)

TraceRunner = Callable[[SkillOptExperimentManifest, str, dict[str, Any]], Mapping[str, Any]]

_SKILLOPT_SECRET_ENVIRONMENT = frozenset(
    {
        "AZURE_OPENAI_API_KEY",
        "OPTIMIZER_AZURE_OPENAI_API_KEY",
        "TARGET_AZURE_OPENAI_API_KEY",
    }
)


@dataclass(frozen=True)
class ExternalSkillOptRuntime:
    """Validated public objects imported from an explicit local SkillOpt package."""

    env_adapter_class: type
    dataloader_class: type
    trainer_class: type
    conversation_builder: Callable[..., dict[str, Any]]
    load_config: Callable[..., dict[str, Any]]
    flatten_config: Callable[[dict[str, Any]], dict[str, Any]]


class SkillOptAcceptanceCandidate(BaseModel):
    """Identity and score of one actually loaded Skill candidate."""

    model_config = ConfigDict(extra="forbid")

    skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    optimization_score: float
    quality_scorecard: QualityScorecard
    efficiency_result: dict[str, Any] = Field(default_factory=dict)


class SkillOptFirstAcceptanceManifest(BaseModel):
    """Human-decision boundary for the first complete optimization flow."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "viknow.skillopt-first-acceptance.v1"
    experiment_id: str
    case_id: str
    oracle_storage_key: str
    scorer_version: str
    baseline: SkillOptAcceptanceCandidate
    candidate: SkillOptAcceptanceCandidate
    comparison: SkillFlowVersionComparison
    decision: str = "pending"
    release_eligible: bool = False


def require_skillopt_package_root(path: Path) -> Path:
    """Validate the explicit external SkillOpt package before altering ``sys.path``."""

    root = path.expanduser().resolve()
    markers = (root / "skillopt/__init__.py", root / "skillopt/engine/trainer.py")
    if not root.is_dir() or not all(marker.is_file() for marker in markers):
        raise FileNotFoundError(f"SkillOpt package root is unavailable or invalid: {root}")
    return root


def load_external_skillopt(path: Path) -> ExternalSkillOptRuntime:
    """Import only the public package boundaries after binding their filesystem origin."""

    root = require_skillopt_package_root(path)
    loaded = sys.modules.get("skillopt")
    if loaded is not None:
        loaded_file = Path(str(getattr(loaded, "__file__", ""))).resolve()
        if not loaded_file.is_relative_to(root):
            raise RuntimeError(f"another SkillOpt package is already loaded: {loaded_file}")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    package = importlib.import_module("skillopt")
    package_file = Path(str(package.__file__)).resolve()
    if not package_file.is_relative_to(root):
        raise RuntimeError(f"SkillOpt import escaped explicit package root: {package_file}")
    base = importlib.import_module("skillopt.envs.base")
    dataloader = importlib.import_module("skillopt.envs.knowledge_qa_v2.dataloader")
    trainer = importlib.import_module("skillopt.engine.trainer")
    conversation = importlib.import_module("skillopt.envs.knowledge_qa_v2.conversation")
    config = importlib.import_module("skillopt.config")
    return ExternalSkillOptRuntime(
        env_adapter_class=base.EnvAdapter,
        dataloader_class=dataloader.KnowledgeQAV2DataLoader,
        trainer_class=trainer.ReflACTTrainer,
        conversation_builder=conversation.build_optimizer_conversation,
        load_config=config.load_config,
        flatten_config=config.flatten_config,
    )


def install_candidate_skill(
    manifest: SkillOptExperimentManifest,
    skill_content: str,
) -> str:
    """Atomically replace only the experiment target Skill and return its digest."""

    source_file = manifest.source_skill_dir / "SKILL.md"
    if _file_sha256(source_file) != manifest.source_skill_sha256:
        raise ValueError("source Skill changed after experiment preparation")
    candidate_file = manifest.experiment_skill_dir / "SKILL.md"
    current_sha256 = _file_sha256(candidate_file)
    validate_skillopt_experiment_copy(
        manifest,
        expected_target_skill_sha256=current_sha256,
    )
    candidate_bytes = skill_content.encode("utf-8")
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    temporary_file = candidate_file.with_name(f".{candidate_file.name}.{uuid4().hex}.tmp")
    try:
        temporary_file.write_bytes(candidate_bytes)
        os.replace(temporary_file, candidate_file)
    finally:
        temporary_file.unlink(missing_ok=True)
    validate_skillopt_experiment_copy(
        manifest,
        expected_target_skill_sha256=candidate_sha256,
    )
    if _file_sha256(source_file) != manifest.source_skill_sha256:
        raise ValueError("candidate installation modified the source Skill")
    return candidate_sha256


def evaluate_candidate_skill(
    *,
    manifest: SkillOptExperimentManifest,
    skill_content: str,
    case: SkillEvalCase,
    accepted_oracle: AcceptedOraclePackage,
    judge: OracleRequirementJudge,
    timeout_seconds: float,
    trace_runner: TraceRunner,
) -> dict[str, Any]:
    """Run and score one candidate without exposing accepted facts to the QA agent."""

    candidate_sha256 = install_candidate_skill(manifest, skill_content)
    selected_assets = case.metadata.get("selected_knowledge_assets") or []
    request = {
        "query": case.question,
        "profile_id": manifest.profile_id,
        "timeout_seconds": timeout_seconds,
        "selected_knowledge_assets": selected_assets,
    }
    trace = trace_runner(manifest, candidate_sha256, request)
    return build_skillopt_rollout_result(
        case=case,
        accepted_oracle=accepted_oracle,
        trace=trace,
        judge=judge,
        target_skill_name=manifest.target_skill_name,
        candidate_sha256=candidate_sha256,
    )


def run_skillopt_acceptance(
    *,
    manifest: SkillOptExperimentManifest,
    skillopt_package_root: Path,
    skillopt_config_path: Path,
    output_dir: Path,
    case: SkillEvalCase,
    accepted_oracle: AcceptedOraclePackage,
    judge: OracleRequirementJudge,
    optimizer_config: Mapping[str, Any],
    optimizer_secret_environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 1800,
    trace_runner: TraceRunner | None = None,
) -> dict[str, Any]:
    """Run the external ReflACT trainer with project-owned rollout and scoring."""

    validate_skillopt_optimizer_config(optimizer_config)
    secret_environment = dict(optimizer_secret_environment or {})
    unexpected_secret_keys = set(secret_environment) - _SKILLOPT_SECRET_ENVIRONMENT
    if unexpected_secret_keys:
        raise ValueError(
            "unsupported SkillOpt secret environment variables: "
            + ", ".join(sorted(unexpected_secret_keys))
        )
    with _temporary_environment(secret_environment):
        runtime = load_external_skillopt(skillopt_package_root)
        output_dir = output_dir.expanduser().resolve()
        if output_dir.exists():
            raise FileExistsError(output_dir)
        split_dir = manifest.experiment_root / "acceptance-inputs"
        _write_single_case_splits(split_dir, case, accepted_oracle)

        structured_config = runtime.load_config(str(skillopt_config_path.expanduser().resolve()))
        config = runtime.flatten_config(structured_config)
        config.update(dict(optimizer_config))
        config.update(
            {
                "env": "knowledge_qa_v2",
                "skill_init": str(manifest.experiment_skill_dir / "SKILL.md"),
                "split_mode": "split_dir",
                "split_dir": str(split_dir),
                "out_root": str(output_dir),
                "num_epochs": 1,
                "train_size": 1,
                "batch_size": 1,
                "accumulation": 1,
                "minibatch_size": 1,
                "merge_batch_size": 1,
                "analyst_workers": 1,
                "sel_env_num": 1,
                "test_env_num": 1,
                "eval_test": True,
                "use_gate": True,
                "use_slow_update": False,
                "use_meta_skill": False,
            }
        )
        adapter = _build_external_adapter(
            runtime=runtime,
            manifest=manifest,
            split_dir=split_dir,
            case=case,
            accepted_oracle=accepted_oracle,
            judge=judge,
            timeout_seconds=timeout_seconds,
            trace_runner=trace_runner or run_isolated_eval_trace,
        )
        summary = dict(runtime.trainer_class(config, adapter).train())
    baseline = next(
        (
            result
            for result in adapter.rollout_records
            if result["candidate_sha256"] == manifest.source_skill_sha256
        ),
        None,
    )
    candidates = [
        result
        for result in adapter.rollout_records
        if result["candidate_sha256"] != manifest.source_skill_sha256
    ]
    if baseline is None:
        raise RuntimeError("SkillOpt acceptance run produced no baseline rollout")
    if not candidates:
        raise RuntimeError("SkillOpt acceptance run produced no distinct candidate rollout")
    acceptance_manifest = build_first_acceptance_manifest(
        experiment_id=manifest.experiment_id,
        accepted_oracle=accepted_oracle,
        baseline=baseline,
        candidate=candidates[-1],
    )
    manifest_path, review_path = write_first_acceptance_artifacts(
        output_dir=output_dir,
        manifest=acceptance_manifest,
    )
    summary["viknow_first_acceptance"] = {
        "manifest_path": str(manifest_path),
        "review_path": str(review_path),
        "decision": acceptance_manifest.decision,
        "release_eligible": acceptance_manifest.release_eligible,
    }
    return summary


def validate_skillopt_optimizer_config(config: Mapping[str, Any]) -> None:
    """Reject credentials that the external Trainer would persist in its snapshots."""

    secret_keys = [
        str(key)
        for key, value in config.items()
        if value
        and any(fragment in str(key).lower() for fragment in ("api_key", "password", "secret"))
    ]
    if secret_keys:
        raise ValueError(
            "SkillOpt credentials must be supplied through the isolated environment: "
            + ", ".join(sorted(secret_keys))
        )


@contextmanager
def _temporary_environment(values: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_external_adapter(
    *,
    runtime: ExternalSkillOptRuntime,
    manifest: SkillOptExperimentManifest,
    split_dir: Path,
    case: SkillEvalCase,
    accepted_oracle: AcceptedOraclePackage,
    judge: OracleRequirementJudge,
    timeout_seconds: float,
    trace_runner: TraceRunner,
) -> Any:
    base_class = runtime.env_adapter_class
    dataloader = runtime.dataloader_class(split_mode="split_dir", split_dir=str(split_dir))
    reference_text = _accepted_reference_text(accepted_oracle)

    class ViKnowAcceptedOracleAdapter(base_class):
        analyst_workers = 1
        failure_only = False
        minibatch_size = 1
        edit_budget = 1

        def __init__(self) -> None:
            self.rollout_records: list[dict[str, Any]] = []

        def setup(self, config: dict[str, Any]) -> None:
            super().setup(config)
            dataloader.setup(config)

        def get_dataloader(self) -> Any:
            return dataloader

        def build_env_from_batch(self, batch: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return list(batch.payload or [])

        def build_train_env(
            self,
            batch_size: int,
            seed: int,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            batch = dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
            return self.build_env_from_batch(batch)

        def build_eval_env(
            self,
            env_num: int,
            split: str,
            seed: int,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            batch = dataloader.build_eval_batch(
                env_num=env_num,
                split=split,
                seed=seed,
                **kwargs,
            )
            return self.build_env_from_batch(batch)

        def rollout(
            self,
            env_manager: list[dict[str, Any]],
            skill_content: str,
            out_dir: str,
            **_kwargs: Any,
        ) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for item in env_manager:
                if str(item.get("id") or "") != case.case_id:
                    raise ValueError("SkillOpt requested an unexpected acceptance case")
                result = evaluate_candidate_skill(
                    manifest=manifest,
                    skill_content=skill_content,
                    case=case,
                    accepted_oracle=accepted_oracle,
                    judge=judge,
                    timeout_seconds=timeout_seconds,
                    trace_runner=trace_runner,
                )
                result.update(
                    {
                        "task_description": case.question,
                        "task_type": str(case.metadata.get("reasoning_type") or "knowledge_qa"),
                        "reference_text": reference_text,
                    }
                )
                _write_skillopt_rollout_artifacts(
                    output_dir=Path(out_dir),
                    item=item,
                    result=result,
                    conversation_builder=runtime.conversation_builder,
                )
                results.append(result)
                self.rollout_records.append(dict(result))
            return results

    return ViKnowAcceptedOracleAdapter()


def run_isolated_eval_trace(
    manifest: SkillOptExperimentManifest,
    candidate_sha256: str,
    request: dict[str, Any],
) -> Mapping[str, Any]:
    """Run one App lifecycle in a fresh process so async pools cannot leak across candidates."""

    worker_root = manifest.experiment_root / "candidate-runs" / uuid4().hex
    worker_root.mkdir(parents=True)
    request_path = worker_root / "request.json"
    response_path = worker_root / "response.json"
    log_path = worker_root / "worker.log"
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "viknow.core.eval.skillopt_candidate_worker",
        "--manifest",
        str(manifest.experiment_root / "manifest.json"),
        "--candidate-sha256",
        candidate_sha256,
        "--request",
        str(request_path),
        "--response",
        str(response_path),
    ]
    environment = dict(os.environ)
    environment["VIKNOW_CONFIG_RELOAD"] = "0"
    for name in _SKILLOPT_SECRET_ENVIRONMENT:
        environment.pop(name, None)
    timeout = float(request.get("timeout_seconds") or 300) + 180
    terminated_after_response = False
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            command,
            cwd=manifest.experiment_root,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        response_ready_at: float | None = None
        while process.poll() is None:
            now = time.monotonic()
            if response_path.is_file():
                response_ready_at = response_ready_at or now
                if now - response_ready_at >= 10:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    terminated_after_response = True
                    break
            if now >= deadline:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.1)
        returncode = process.returncode
    if returncode != 0 and not terminated_after_response:
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"isolated SkillOpt worker failed with exit {returncode}: {log_tail}")
    if not response_path.is_file():
        raise RuntimeError("isolated SkillOpt worker produced no response")
    payload = json.loads(response_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("isolated SkillOpt eval returned a non-object trace")
    return payload


def _write_single_case_splits(
    split_dir: Path,
    case: SkillEvalCase,
    accepted_oracle: AcceptedOraclePackage,
) -> None:
    if split_dir.exists():
        raise FileExistsError(split_dir)
    item = {
        "id": case.case_id,
        "skill": "knowledge-qa-v2",
        "library_id": accepted_oracle.scope.library_id,
        "question": case.question,
        "reference_answer": _accepted_reference_text(accepted_oracle),
        "expected_video_segments": _accepted_video_segments(accepted_oracle),
        "task_type": str(case.metadata.get("reasoning_type") or "knowledge_qa"),
    }
    for split in ("train", "val", "test"):
        path = split_dir / split
        path.mkdir(parents=True)
        path.joinpath("items.json").write_text(
            json.dumps([item], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _write_skillopt_rollout_artifacts(
    *,
    output_dir: Path,
    item: dict[str, Any],
    result: dict[str, Any],
    conversation_builder: Callable[..., dict[str, Any]],
) -> None:
    prediction_dir = output_dir / "predictions" / str(item["id"])
    prediction_dir.mkdir(parents=True, exist_ok=True)
    trajectory = dict(result["trajectory"])
    conversation = conversation_builder(item, trajectory, result)
    for name, payload in (
        ("trace.json", trajectory),
        ("scored.json", result),
        ("conversation.json", conversation),
    ):
        prediction_dir.joinpath(name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _accepted_reference_text(accepted_oracle: AcceptedOraclePackage) -> str:
    if accepted_oracle.reference_answer:
        return accepted_oracle.reference_answer
    if accepted_oracle.human_revision and accepted_oracle.human_revision.revised_answer:
        return accepted_oracle.human_revision.revised_answer
    return "\n".join(fact.statement for fact in accepted_oracle.facts)


def _accepted_video_segments(accepted_oracle: AcceptedOraclePackage) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for fact in accepted_oracle.facts:
        for evidence_ref in fact.evidence_refs:
            time_range = evidence_ref.get("time_range")
            if not isinstance(time_range, Mapping):
                continue
            segments.append(
                {
                    "segment_id": str(
                        evidence_ref.get("segment_id")
                        or evidence_ref.get("evidence_id")
                        or fact.fact_id
                    ),
                    "start_seconds": time_range.get("start_seconds"),
                    "end_seconds": time_range.get("end_seconds"),
                }
            )
    return segments


def _acceptance_candidate(payload: Mapping[str, Any]) -> SkillOptAcceptanceCandidate:
    trajectory = _as_mapping(payload.get("trajectory"))
    scorecard = QualityScorecard.model_validate(payload.get("quality_scorecard"))
    if scorecard.optimization_score is None:
        raise ValueError("acceptance candidate has no optimization score")
    return SkillOptAcceptanceCandidate(
        skill_sha256=str(payload.get("candidate_sha256") or ""),
        snapshot_skill_sha256=str(payload.get("snapshot_skill_sha256") or ""),
        run_id=str(trajectory.get("run_id") or ""),
        trace_id=str(trajectory.get("trace_id") or ""),
        status=str(payload.get("status") or trajectory.get("status") or "unknown"),
        optimization_score=scorecard.optimization_score,
        quality_scorecard=scorecard,
        efficiency_result=_as_mapping(payload.get("efficiency_result")),
    )


def _comparison_run(payload: Mapping[str, Any]) -> dict[str, Any]:
    trajectory = _as_mapping(payload.get("trajectory"))
    return {
        "case_id": str(payload.get("case_id") or payload.get("id") or ""),
        "status": str(payload.get("status") or trajectory.get("status") or "unknown"),
        "run_id": trajectory.get("run_id"),
        "trace_id": trajectory.get("trace_id"),
        "quality_scorecard": payload.get("quality_scorecard"),
        "efficiency_result": payload.get("efficiency_result"),
    }


def load_acceptance_case(
    *,
    cases_path: Path,
    case_id: str,
    accepted_oracle_path: Path,
) -> tuple[SkillEvalCase, AcceptedOraclePackage]:
    """Load exactly one case and its human-accepted Oracle package."""

    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("cases file must contain a list of cases")
    matches = [
        item for item in raw_cases if isinstance(item, Mapping) and item.get("case_id") == case_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one case with case_id {case_id!r}")
    case = SkillEvalCase.model_validate(matches[0])
    accepted = AcceptedOraclePackage.model_validate_json(accepted_oracle_path.read_bytes())
    if accepted.case_id != case.case_id:
        raise ValueError("accepted Oracle case_id differs from evaluation case")
    return case, accepted


def build_skillopt_rollout_result(
    *,
    case: SkillEvalCase,
    accepted_oracle: AcceptedOraclePackage,
    trace: Mapping[str, Any] | Any,
    judge: OracleRequirementJudge,
    target_skill_name: str,
    candidate_sha256: str,
) -> dict[str, Any]:
    """Score one actual candidate run and prove that runtime loaded that candidate."""

    payload = _as_mapping(trace)
    _require_candidate_snapshot(
        payload,
        target_skill_name=target_skill_name,
        candidate_sha256=candidate_sha256,
    )
    oracle_package = accepted_oracle_as_evidence_package(accepted_oracle)
    result = score_accepted_trace(
        case=case,
        trace=payload,
        oracle_package=oracle_package,
        judge=judge,
    )
    if result.quality_scorecard is None:
        raise ValueError("accepted trace did not produce a quality scorecard")
    scorecard = QualityScorecard.model_validate(result.quality_scorecard)
    artifact = build_skillopt_evaluation_artifact(
        case_id=case.case_id,
        quality_scorecard=scorecard,
        trace=payload,
        evaluation_stage="accepted",
        release_eligible=False,
        oracle_storage_key=oracle_package.storage_key,
    )
    projected = skillopt_evaluator_result(artifact)
    projected.update(
        {
            "case_id": case.case_id,
            "status": artifact.trace.status,
            "candidate_sha256": candidate_sha256,
            "snapshot_skill_sha256": candidate_sha256,
            "quality_scorecard": result.quality_scorecard,
            "efficiency_result": result.efficiency_result,
            "trajectory": {
                "case_id": case.case_id,
                "question": case.question,
                "run_id": artifact.trace.run_id,
                "trace_id": artifact.trace.trace_id,
                "status": artifact.trace.status,
                "final_answer": artifact.trace.answer,
                "tool_call_chain": artifact.trace.tool_call_chain,
            },
        }
    )
    return projected


def build_first_acceptance_manifest(
    *,
    experiment_id: str,
    accepted_oracle: AcceptedOraclePackage,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> SkillOptFirstAcceptanceManifest:
    """Build an accepted-stage comparison without choosing keep or reject."""

    baseline_record = _acceptance_candidate(baseline)
    candidate_record = _acceptance_candidate(candidate)
    oracle_package = accepted_oracle_as_evidence_package(accepted_oracle)
    provenance = AcceptedComparisonProvenance(
        oracle_storage_key=oracle_package.storage_key,
        baseline_skill_sha256=baseline_record.skill_sha256,
        candidate_skill_sha256=candidate_record.skill_sha256,
        baseline_snapshot_skill_sha256=baseline_record.snapshot_skill_sha256,
        candidate_snapshot_skill_sha256=candidate_record.snapshot_skill_sha256,
        baseline_run_id=baseline_record.run_id,
        candidate_run_id=candidate_record.run_id,
        baseline_trace_id=baseline_record.trace_id,
        candidate_trace_id=candidate_record.trace_id,
    )
    comparison = compare_accepted_quality_run_payloads(
        baseline=[_comparison_run(baseline)],
        candidate=[_comparison_run(candidate)],
        provenance=provenance,
    )
    return SkillOptFirstAcceptanceManifest(
        experiment_id=experiment_id,
        case_id=accepted_oracle.case_id,
        oracle_storage_key=oracle_package.storage_key,
        scorer_version=comparison.scorer_version,
        baseline=baseline_record,
        candidate=candidate_record,
        comparison=comparison,
    )


def render_first_acceptance_review(manifest: SkillOptFirstAcceptanceManifest) -> str:
    """Render the concise, human-owned keep/reject decision material."""

    delta = manifest.comparison.optimization_score_delta
    delta_text = "不可计算" if delta is None else f"{delta:+.2f}"
    baseline = manifest.baseline
    candidate = manifest.candidate
    score_rows = [
        _review_score_row(
            "答案与证据",
            baseline.quality_scorecard.answer_evidence.score,
            candidate.quality_scorecard.answer_evidence.score,
        ),
        _review_score_row(
            "流程合规",
            baseline.quality_scorecard.process_compliance.score,
            candidate.quality_scorecard.process_compliance.score,
        ),
        _review_score_row(
            "效率与收敛",
            baseline.quality_scorecard.efficiency_convergence.score,
            candidate.quality_scorecard.efficiency_convergence.score,
        ),
        _review_score_row(
            "发布安全",
            baseline.quality_scorecard.release_safety.score,
            candidate.quality_scorecard.release_safety.score,
        ),
        f"| 总分 | {baseline.optimization_score:.2f} | "
        f"{candidate.optimization_score:.2f} | {delta_text} |",
    ]
    execution_rows = [
        _review_metric_row("运行终态", baseline.status, candidate.status),
        _review_metric_row(
            "耗时（秒）",
            baseline.efficiency_result.get("latency_seconds"),
            candidate.efficiency_result.get("latency_seconds"),
            precision=2,
        ),
        _review_metric_row(
            "工具调用次数",
            baseline.efficiency_result.get("tool_call_count"),
            candidate.efficiency_result.get("tool_call_count"),
        ),
        _review_metric_row(
            "输入 Token",
            baseline.efficiency_result.get("token_input"),
            candidate.efficiency_result.get("token_input"),
        ),
        _review_metric_row(
            "输出 Token",
            baseline.efficiency_result.get("token_output"),
            candidate.efficiency_result.get("token_output"),
        ),
    ]
    return "\n".join(
        (
            "# SkillOpt 首次验收人工决策",
            "",
            f"- 实验：`{manifest.experiment_id}`",
            f"- Case：`{manifest.case_id}`",
            f"- 评分器：`{manifest.scorer_version}`",
            f"- Baseline：`{baseline.skill_sha256}`",
            f"  - run：`{baseline.run_id}`；trace：`{baseline.trace_id}`",
            f"- Candidate：`{candidate.skill_sha256}`",
            f"  - run：`{candidate.run_id}`；trace：`{candidate.trace_id}`",
            "",
            "## 质量评分",
            "",
            "| 维度 | Baseline | Candidate | 变化 |",
            "| --- | ---: | ---: | ---: |",
            *score_rows,
            "",
            "## 运行事实",
            "",
            "| 指标 | Baseline | Candidate | 变化 |",
            "| --- | ---: | ---: | ---: |",
            *execution_rows,
            "",
            "答案与证据维度必须单独审核；总分提高不能替代答案正确性判断。",
            "",
            f"- 人工决定：`{manifest.decision}`（可填写 `keep` 或 `reject`）",
            "- 发布资格：`false`；本材料不会自动发布或覆盖正式 Skill。",
            "",
        )
    )


def _review_score_row(label: str, baseline: float | None, candidate: float | None) -> str:
    baseline_text = _review_number(baseline, scale=100, precision=2)
    candidate_text = _review_number(candidate, scale=100, precision=2)
    delta = None if baseline is None or candidate is None else (candidate - baseline) * 100
    delta_text = _review_number(delta, signed=True, precision=2)
    return f"| {label} | {baseline_text} | {candidate_text} | {delta_text} |"


def _review_metric_row(
    label: str,
    baseline: Any,
    candidate: Any,
    *,
    precision: int = 0,
) -> str:
    if isinstance(baseline, int | float) and isinstance(candidate, int | float):
        baseline_text = _review_number(float(baseline), precision=precision)
        candidate_text = _review_number(float(candidate), precision=precision)
        delta_text = _review_number(
            float(candidate) - float(baseline),
            signed=True,
            precision=precision,
        )
    else:
        baseline_text = "缺失" if baseline is None else str(baseline)
        candidate_text = "缺失" if candidate is None else str(candidate)
        delta_text = "—"
    return f"| {label} | {baseline_text} | {candidate_text} | {delta_text} |"


def _review_number(
    value: float | None,
    *,
    scale: float = 1,
    signed: bool = False,
    precision: int = 0,
) -> str:
    if value is None:
        return "缺失"
    spec = f"{'+' if signed else ''}.{precision}f"
    return format(value * scale, spec)


def write_first_acceptance_artifacts(
    *,
    output_dir: Path,
    manifest: SkillOptFirstAcceptanceManifest,
) -> tuple[Path, Path]:
    """Persist the machine-readable comparison and human decision template."""

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "viknow-first-acceptance.json"
    review_path = output_dir / "viknow-first-acceptance-review.md"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    review_path.write_text(render_first_acceptance_review(manifest), encoding="utf-8")
    return manifest_path, review_path


def _require_candidate_snapshot(
    trace: Mapping[str, Any],
    *,
    target_skill_name: str,
    candidate_sha256: str,
) -> None:
    metadata = _as_mapping(trace.get("metadata"))
    snapshot = _as_mapping(metadata.get("behavior_snapshot"))
    skills = snapshot.get("skills")
    if not isinstance(skills, list):
        raise ValueError("behavior snapshot does not contain loaded Skills")
    matching = [
        _as_mapping(item)
        for item in skills
        if isinstance(item, Mapping)
        and str(item.get("id") or item.get("name") or "") == target_skill_name
    ]
    if len(matching) != 1 or str(matching[0].get("sha256") or "") != candidate_sha256:
        raise ValueError("candidate Skill hash mismatch in behavior snapshot")


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()
