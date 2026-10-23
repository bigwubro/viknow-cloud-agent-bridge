"""Provisional SkillOpt scoring for a human-approved Gate A optimization direction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.skillopt_acceptance import _require_candidate_snapshot
from viknow.core.eval.skillopt_experiment import SkillOptExperimentManifest
from viknow.core.learning.case_review import ApprovedCaseSnapshot


class GateAOptimizationJudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    question: str
    baseline_answer: str | None = None
    candidate_answer: str
    reference_answer: str | None = None
    issue_summary: str
    optimization_suggestion: str
    trace_summary: dict[str, Any]


class GateAOptimizationAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0, le=1)
    direction_satisfied: bool
    reason: str
    confidence: float = Field(ge=0, le=1)


class GateAOptimizationJudge(Protocol):
    model: str

    def evaluate(self, request: GateAOptimizationJudgeRequest) -> GateAOptimizationAssessment: ...


class ConfiguredGateAOptimizationJudge:
    def __init__(self, *, chat_model: Any, model: str, max_tokens: int) -> None:
        self._chat_model = chat_model
        self.model = model
        self._max_tokens = max_tokens

    def evaluate(self, request: GateAOptimizationJudgeRequest) -> GateAOptimizationAssessment:
        from viknow.core.multimodal.structured_llm import invoke_structured_llm

        prompt = """你是 Skill 优化实验的方向评估器。只判断候选回答是否相对原回答改善了
人工批准的 issue_summary 和 optimization_suggestion。不要评价未提供证据的事实正确性。
direction_satisfied 只在优化方向被完整满足时为 true；score 表示改善程度，范围 0 到 1。
输入：""" + json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        result = invoke_structured_llm(
            self._chat_model,
            GateAOptimizationAssessment,
            prompt,
            run_name="skillopt_gate_a_direction_judge",
            max_attempts=1,
            max_tokens=self._max_tokens,
        )
        if result is None:
            raise ValueError("Gate A optimization judge returned no valid result")
        return result


def build_gate_a_optimization_judge(model_config: Any) -> ConfiguredGateAOptimizationJudge:
    from langchain_openai import ChatOpenAI

    return ConfiguredGateAOptimizationJudge(
        chat_model=ChatOpenAI(
            model=model_config.require_model(),
            base_url=model_config.resolve_base_url(),
            api_key=model_config.require_api_key(),
            temperature=model_config.temperature,
            timeout=model_config.timeout_seconds,
            max_retries=0,
        ),
        model=model_config.require_model(),
        max_tokens=model_config.max_tokens,
    )


class GateASkillOptCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_sha256: str
    snapshot_skill_sha256: str
    run_id: str
    trace_id: str
    status: str
    soft: float = Field(ge=0, le=1)
    hard: Literal[0, 1]
    direction_assessment: GateAOptimizationAssessment
    efficiency_result: dict[str, Any] = Field(default_factory=dict)


class GateASkillOptAcceptanceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["viknow.skillopt-gate-a-acceptance.v1"] = (
        "viknow.skillopt-gate-a-acceptance.v1"
    )
    experiment_id: str
    case_id: str
    gate_a_snapshot_sha256: str
    judge_model: str
    baseline: GateASkillOptCandidate
    candidate: GateASkillOptCandidate
    score_delta: float
    decision: Literal["pending"] = "pending"
    evaluation_stage: Literal["provisional"] = "provisional"
    release_eligible: Literal[False] = False


def score_gate_a_snapshot_trace(
    *,
    snapshot: ApprovedCaseSnapshot,
    trace: Mapping[str, Any],
    candidate_sha256: str,
    target_skill_name: str,
    judge: GateAOptimizationJudge,
    reference_answer: str | None = None,
) -> dict[str, Any]:
    _require_candidate_snapshot(
        trace,
        target_skill_name=target_skill_name,
        candidate_sha256=candidate_sha256,
    )
    proposal = snapshot.optimization_proposal
    analysis = proposal.analysis if proposal is not None else None
    if analysis is None:
        raise ValueError("Gate A snapshot has no approved optimization analysis")
    answer = str(trace.get("answer") or "")
    assessment = judge.evaluate(
        GateAOptimizationJudgeRequest(
            case_id=snapshot.run_id,
            question=snapshot.question,
            baseline_answer=snapshot.answer,
            candidate_answer=answer,
            reference_answer=reference_answer,
            issue_summary=analysis.issue_summary,
            optimization_suggestion=analysis.optimization_suggestion,
            trace_summary={
                "status": str(trace.get("status") or "unknown"),
                "tool_call_records": list(
                    (trace.get("metadata") or {}).get("tool_call_records") or []
                ),
            },
        )
    )
    metadata = trace.get("metadata") or {}
    return {
        "id": snapshot.run_id,
        "soft": assessment.score,
        "hard": int(assessment.direction_satisfied),
        "breakdown": {
            "evaluation_stage": "provisional",
            "release_eligible": False,
            "score_basis": "gate_a_optimization_direction",
            "judge_reason": assessment.reason,
            "optimizer_feedback": {
                "issue_summary": analysis.issue_summary,
                "optimization_suggestion": analysis.optimization_suggestion,
            },
        },
        "failure_reasons": [] if assessment.direction_satisfied else [assessment.reason],
        "candidate_sha256": candidate_sha256,
        "snapshot_skill_sha256": candidate_sha256,
        "run_id": str(trace.get("run_id") or ""),
        "trace_id": str(trace.get("langfuse_trace_id") or ""),
        "status": str(trace.get("status") or "unknown"),
        "direction_assessment": assessment.model_dump(mode="json"),
        "efficiency_result": {},
        "trajectory": {
            "case_id": snapshot.run_id,
            "question": snapshot.question,
            "run_id": trace.get("run_id"),
            "trace_id": trace.get("langfuse_trace_id"),
            "status": trace.get("status"),
            "final_answer": answer,
            "tool_call_chain": list(metadata.get("tool_call_records") or []),
        },
    }


def build_gate_a_acceptance_manifest(
    *,
    experiment_id: str,
    snapshot: ApprovedCaseSnapshot,
    judge_model: str,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> GateASkillOptAcceptanceManifest:
    candidate_fields = GateASkillOptCandidate.model_fields
    baseline_model = GateASkillOptCandidate.model_validate(
        {name: baseline[name] for name in candidate_fields if name in baseline}
    )
    candidate_model = GateASkillOptCandidate.model_validate(
        {name: candidate[name] for name in candidate_fields if name in candidate}
    )
    snapshot_payload = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return GateASkillOptAcceptanceManifest(
        experiment_id=experiment_id,
        case_id=snapshot.run_id,
        gate_a_snapshot_sha256=hashlib.sha256(snapshot_payload).hexdigest(),
        judge_model=judge_model,
        baseline=baseline_model,
        candidate=candidate_model,
        score_delta=round(candidate_model.soft - baseline_model.soft, 6),
    )


def run_skillopt_gate_a_snapshot(
    *,
    manifest: SkillOptExperimentManifest,
    skillopt_package_root: Path,
    skillopt_config_path: Path,
    output_dir: Path,
    snapshot: ApprovedCaseSnapshot,
    judge: GateAOptimizationJudge,
    optimizer_config: Mapping[str, Any],
    optimizer_secret_environment: Mapping[str, str],
    timeout_seconds: float,
    reference_answer: str | None = None,
) -> dict[str, Any]:
    """Run one-epoch SkillOpt training with provisional Gate A direction scoring."""

    from viknow.core.eval.skillopt_acceptance import (
        _temporary_environment,
        _write_skillopt_rollout_artifacts,
        install_candidate_skill,
        load_external_skillopt,
        run_isolated_eval_trace,
        validate_skillopt_optimizer_config,
    )

    validate_skillopt_optimizer_config(optimizer_config)
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)
    split_dir = manifest.experiment_root / "gate-a-inputs"
    item = {
        "id": snapshot.run_id,
        "skill": manifest.target_skill_name,
        "question": snapshot.question,
        "reference_answer": reference_answer or "",
        "task_type": "knowledge_qa",
    }
    for split in ("train", "val", "test"):
        path = split_dir / split
        path.mkdir(parents=True)
        path.joinpath("items.json").write_text(
            json.dumps([item], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    with _temporary_environment(optimizer_secret_environment):
        runtime = load_external_skillopt(skillopt_package_root)
        structured = runtime.load_config(str(skillopt_config_path.expanduser().resolve()))
        config = runtime.flatten_config(structured)
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
                "eval_test": False,
                "use_gate": True,
                "use_slow_update": False,
                "use_meta_skill": False,
            }
        )
        dataloader = runtime.dataloader_class(split_mode="split_dir", split_dir=str(split_dir))
        records: list[dict[str, Any]] = []
        base_class = runtime.env_adapter_class

        class GateAAdapter(base_class):
            analyst_workers = 1
            failure_only = False
            minibatch_size = 1
            edit_budget = 1

            def setup(self, cfg: dict[str, Any]) -> None:
                super().setup(cfg)
                dataloader.setup(cfg)

            def get_dataloader(self) -> Any:
                return dataloader

            def build_env_from_batch(self, batch: Any, **_kwargs: Any) -> list[dict[str, Any]]:
                return list(batch.payload or [])

            def build_train_env(self, batch_size: int, seed: int, **kwargs: Any) -> list[dict]:
                return self.build_env_from_batch(
                    dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
                )

            def build_eval_env(
                self, env_num: int, split: str, seed: int, **kwargs: Any
            ) -> list[dict]:
                return self.build_env_from_batch(
                    dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed, **kwargs)
                )

            def rollout(
                self, env_manager: list[dict], skill_content: str, out_dir: str, **_kwargs: Any
            ) -> list[dict]:
                results = []
                for current_item in env_manager:
                    candidate_sha256 = install_candidate_skill(manifest, skill_content)
                    selected_assets = snapshot.behavior_snapshot.get(
                        "knowledge_start_state", {}
                    ).get("selected_knowledge_assets", [])
                    trace = run_isolated_eval_trace(
                        manifest,
                        candidate_sha256,
                        {
                            "query": snapshot.question,
                            "profile_id": manifest.profile_id,
                            "timeout_seconds": timeout_seconds,
                            "selected_knowledge_assets": selected_assets,
                        },
                    )
                    result = score_gate_a_snapshot_trace(
                        snapshot=snapshot,
                        trace=trace,
                        candidate_sha256=candidate_sha256,
                        target_skill_name=manifest.target_skill_name,
                        judge=judge,
                    )
                    result.update(
                        {
                            "task_description": snapshot.question,
                            "task_type": "knowledge_qa",
                            "reference_text": analysis.optimization_suggestion,
                        }
                    )
                    _write_skillopt_rollout_artifacts(
                        output_dir=Path(out_dir),
                        item=current_item,
                        result=result,
                        conversation_builder=runtime.conversation_builder,
                    )
                    records.append(dict(result))
                    results.append(result)
                return results

        proposal = snapshot.optimization_proposal
        analysis = proposal.analysis if proposal else None
        if analysis is None:
            raise ValueError("Gate A snapshot has no optimization analysis")
        summary = dict(runtime.trainer_class(config, GateAAdapter()).train())

    baseline = next(
        (r for r in records if r["candidate_sha256"] == manifest.source_skill_sha256), None
    )
    candidates = [r for r in records if r["candidate_sha256"] != manifest.source_skill_sha256]
    if baseline is None or not candidates:
        raise RuntimeError("Gate A SkillOpt run did not produce baseline and candidate rollouts")
    acceptance = build_gate_a_acceptance_manifest(
        experiment_id=manifest.experiment_id,
        snapshot=snapshot,
        judge_model=judge.model,
        baseline=baseline,
        candidate=candidates[-1],
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "viknow-gate-a-acceptance.json"
    manifest_path.write_text(acceptance.model_dump_json(indent=2), encoding="utf-8")
    summary["viknow_gate_a_acceptance"] = {"manifest_path": str(manifest_path)}
    return summary
