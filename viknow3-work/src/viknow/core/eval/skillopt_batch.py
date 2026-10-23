"""One external SkillOpt trainer over a frozen mixed Gate A / accepted Oracle dataset."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from viknow.core.eval.candidate_evidence import AcceptedOraclePackage
from viknow.core.eval.skill_flow import SkillEvalCase
from viknow.core.eval.skillopt_acceptance import (
    _accepted_reference_text,
    _temporary_environment,
    _write_skillopt_rollout_artifacts,
    build_skillopt_rollout_result,
    install_candidate_skill,
    load_external_skillopt,
    run_isolated_eval_trace,
    validate_skillopt_optimizer_config,
)
from viknow.core.eval.skillopt_gate_a import score_gate_a_snapshot_trace
from viknow.core.learning.case_review import SkillOptimizationExperiment


def run_skillopt_batch(
    *,
    experiment: SkillOptimizationExperiment,
    manifest: Any,
    skillopt_package_root: Path,
    output_dir: Path,
    optimizer_config: Mapping[str, Any],
    optimizer_secret_environment: Mapping[str, str],
    oracle_judge: Any,
    gate_a_judge: Any,
    timeout_seconds: float,
    progress_callback: Callable[[dict], None],
    trace_runner: Callable = run_isolated_eval_trace,
) -> dict[str, Any]:
    validate_skillopt_optimizer_config(optimizer_config)
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True)
    split_dir = manifest.experiment_root / "batch-inputs"
    cases = {case.run_id: case for case in experiment.cases}
    accepted = {
        case.run_id: AcceptedOraclePackage.model_validate(case.accepted_oracle_package)
        for case in experiment.cases
        if case.accepted_oracle_package is not None
    }
    for case_id, package in accepted.items():
        if package.case_id != case_id:
            raise ValueError("accepted Oracle case_id differs from frozen case")
    references = {
        case.run_id: case.reference_answer
        if case.reference_answer is not None
        else (_accepted_reference_text(accepted[case.run_id]) if case.run_id in accepted else "")
        for case in experiment.cases
    }
    items = [
        {
            "id": case.run_id,
            "skill": experiment.target_skill_id,
            "question": case.gate_a_snapshot.question,
            "reference_answer": references[case.run_id],
            "task_type": "knowledge_qa",
        }
        for case in experiment.cases
    ]
    # All three splits deliberately cover the selected cases. This is an optimization
    # comparison on the reviewed corpus, not a claim of independent holdout validation.
    for split in ("train", "val", "test"):
        path = split_dir / split
        path.mkdir(parents=True)
        (path / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    records = []
    started = time.monotonic()
    sequence = 0

    def emit(event):
        nonlocal sequence
        sequence += 1
        event = {"sequence": sequence, "elapsed_seconds": time.monotonic() - started, **event}
        with (output_dir / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        progress_callback(event)

    with _temporary_environment(optimizer_secret_environment):
        runtime = load_external_skillopt(skillopt_package_root)
        config = dict(optimizer_config)
        config.update(experiment.config.advanced_parameters)
        config.update(
            {
                "env": "knowledge_qa_v2",
                "skill_init": str(manifest.experiment_skill_dir / "SKILL.md"),
                "split_mode": "split_dir",
                "split_dir": str(split_dir),
                "out_root": str(output_dir),
                "num_epochs": experiment.config.epochs,
                "batch_size": experiment.config.batch_size,
                "n_samples": experiment.config.n_samples,
                "train_size": len(items),
                "sel_env_num": len(items),
                "test_env_num": len(items),
                "eval_test": True,
                "acceptance_metric": experiment.config.acceptance_metric,
                "acceptance_min_delta": experiment.config.acceptance_min_delta,
                "accumulation": 1,
                "use_gate": True,
            }
        )
        dataloader = runtime.dataloader_class(split_mode="split_dir", split_dir=str(split_dir))

        class BatchAdapter(runtime.env_adapter_class):
            def setup(self, cfg):
                super().setup(cfg)
                dataloader.setup(cfg)
                self.analyst_workers = cfg["analyst_workers"]
                self.failure_only = cfg["failure_only"]
                self.minibatch_size = cfg["minibatch_size"]
                self.edit_budget = cfg["edit_budget"]

            def get_dataloader(self):
                return dataloader

            def build_env_from_batch(self, batch, **kwargs):
                return list(batch.payload or [])

            def build_train_env(self, batch_size, seed, **kwargs):
                return self.build_env_from_batch(
                    dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
                )

            def build_eval_env(self, env_num, split, seed, **kwargs):
                return self.build_env_from_batch(
                    dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed, **kwargs)
                )

            def rollout(self, env_manager, skill_content, out_dir, **kwargs):
                results = []
                sha = install_candidate_skill(manifest, skill_content)
                for item in env_manager:
                    case = cases[item["id"]]
                    snapshot = case.gate_a_snapshot
                    case_started = time.monotonic()
                    stage = "baseline" if sha == manifest.source_skill_sha256 else "training"
                    log_index = (
                        str(Path(out_dir).relative_to(manifest.experiment_root))
                        if Path(out_dir).is_relative_to(manifest.experiment_root)
                        else None
                    )
                    emit(
                        {
                            "stage": stage,
                            "case_id": case.run_id,
                            "status": "running",
                            "candidate_sha256": sha,
                            "log_index": log_index,
                        }
                    )
                    trace = trace_runner(
                        manifest,
                        sha,
                        {
                            "query": snapshot.question,
                            "profile_id": manifest.profile_id,
                            "agent_model": experiment.config.target_model,
                            "timeout_seconds": timeout_seconds,
                            "selected_knowledge_assets": snapshot.behavior_snapshot.get(
                                "knowledge_start_state", {}
                            ).get("selected_knowledge_assets", []),
                        },
                    )
                    samples = []
                    for _ in range(experiment.config.n_samples):
                        if case.run_id in accepted:
                            result = build_skillopt_rollout_result(
                                case=SkillEvalCase(
                                    case_id=case.run_id,
                                    question=snapshot.question,
                                    reference_answer=references[case.run_id],
                                ),
                                accepted_oracle=accepted[case.run_id].model_copy(
                                    update={"reference_answer": references[case.run_id]}
                                ),
                                trace=trace,
                                judge=oracle_judge,
                                target_skill_name=manifest.target_skill_name,
                                candidate_sha256=sha,
                            )
                        else:
                            result = score_gate_a_snapshot_trace(
                                snapshot=snapshot,
                                trace=trace,
                                candidate_sha256=sha,
                                target_skill_name=manifest.target_skill_name,
                                judge=gate_a_judge,
                                reference_answer=references[case.run_id],
                            )
                        samples.append(result)
                    result = dict(samples[0])
                    result.update(
                        soft=sum(s["soft"] for s in samples) / len(samples),
                        hard=int(all(s["hard"] for s in samples)),
                        judge_samples=[{"soft": s["soft"], "hard": s["hard"]} for s in samples],
                        task_description=snapshot.question,
                        task_type="knowledge_qa",
                        reference_text=references[case.run_id],
                    )
                    _write_skillopt_rollout_artifacts(
                        output_dir=Path(out_dir),
                        item=item,
                        result=result,
                        conversation_builder=runtime.conversation_builder,
                    )
                    records.append(result)
                    results.append(result)
                    emit(
                        {
                            "stage": stage,
                            "case_id": case.run_id,
                            "status": "completed",
                            "candidate_sha256": sha,
                            "soft": result["soft"],
                            "hard": result["hard"],
                            "wall_time_seconds": time.monotonic() - case_started,
                            "run_id": result.get("run_id"),
                            "trace_id": result.get("trace_id"),
                            "log_index": log_index,
                        }
                    )
                return results

        emit({"stage": "training", "case_count": len(items)})
        adapter = BatchAdapter()
        summary = dict(runtime.trainer_class(config, adapter).train())
        best_content = (output_dir / "best_skill.md").read_text(encoding="utf-8")
        best_sha = hashlib.sha256(best_content.encode()).hexdigest()
        baseline_by_id = {
            r["id"]: r for r in records if r["candidate_sha256"] == manifest.source_skill_sha256
        }
        candidate_by_id = {r["id"]: r for r in records if r["candidate_sha256"] == best_sha}
        if set(baseline_by_id) != set(cases) or set(candidate_by_id) != set(cases):
            raise RuntimeError(
                "batch acceptance requires baseline and selected candidate scores "
                "for every frozen case"
            )
        metric = experiment.config.acceptance_metric
        comparisons = [
            {
                "case_id": case_id,
                "baseline": baseline_by_id[case_id],
                "candidate": candidate_by_id[case_id],
                "delta": candidate_by_id[case_id][metric] - baseline_by_id[case_id][metric],
            }
            for case_id in cases
        ]
        delta = sum(c["delta"] for c in comparisons) / len(comparisons)
        acceptance = {
            "schema_version": "viknow.skillopt-batch-acceptance.v1",
            "experiment_id": manifest.experiment_id,
            "metric": metric,
            "min_delta": experiment.config.acceptance_min_delta,
            "score_delta": delta,
            "threshold_met": delta >= experiment.config.acceptance_min_delta,
            "candidate_sha256": best_sha,
            "baseline_sha256": manifest.source_skill_sha256,
            "cases": comparisons,
            "decision": "pending",
            "release_eligible": False,
            "evaluation_scope": "selected_case_corpus",
        }
        (output_dir / "batch-acceptance.json").write_text(
            json.dumps(acceptance, ensure_ascii=False), encoding="utf-8"
        )
        emit({"stage": "pending_acceptance", "score_delta": delta})
    # The trainer's config contains server connection settings; keep those out of the DB/API.
    summary.pop("config", None)
    return {
        "experiment_id": manifest.experiment_id,
        "result_summary": summary,
        "acceptance_manifest": acceptance,
    }
