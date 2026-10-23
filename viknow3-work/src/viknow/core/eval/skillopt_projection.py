"""Stable projection of Skill Flow quality scores for an optimizer client."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.quality_score import QualityScorecard


class SkillOptScoreProjection(BaseModel):
    """A score package whose only numeric quality source is ``QualityScorecard``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skillopt-score-projection.v1"] = (
        "viknow.skillopt-score-projection.v1"
    )
    case_id: str
    soft: float = Field(ge=0.0, le=1.0)
    hard: Literal[0, 1]
    breakdown: dict[str, object]


class SkillOptTraceProjection(BaseModel):
    """Content-bounded runtime facts required by offline optimization evaluation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skillopt-trace-projection.v1"] = (
        "viknow.skillopt-trace-projection.v1"
    )
    status: str
    answer: str
    run_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, object]
    tool_call_chain: list[dict[str, object]]


class SkillOptEvaluationArtifact(BaseModel):
    """One case's score and bounded trace, ready for a future optimizer bridge."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skillopt-evaluation-artifact.v1"] = (
        "viknow.skillopt-evaluation-artifact.v1"
    )
    score: SkillOptScoreProjection
    trace: SkillOptTraceProjection


def optimizer_complete_answer(scorecard: QualityScorecard) -> bool:
    """Classify whether every evaluable answer requirement is fully satisfied."""

    evaluable_rules = [rule for rule in scorecard.answer_evidence.rules if rule.score is not None]
    return (
        bool(evaluable_rules)
        and scorecard.not_evaluable_requirement_count == 0
        and all(rule.score == 1.0 for rule in scorecard.answer_evidence.rules)
    )


def project_quality_scorecard(
    *,
    case_id: str,
    quality_scorecard: QualityScorecard,
    evaluation_stage: Literal["provisional", "accepted"],
    release_eligible: bool,
    oracle_storage_key: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
) -> SkillOptScoreProjection:
    """Project an already-scored case without recomputing its quality dimensions."""

    if evaluation_stage == "provisional" and release_eligible:
        raise ValueError("provisional cases cannot be release eligible")
    if evaluation_stage == "accepted" and not oracle_storage_key:
        raise ValueError("accepted cases require accepted Oracle provenance")
    optimization_score = quality_scorecard.optimization_score
    if optimization_score is None:
        raise ValueError("quality_scorecard has no evaluable optimization_score")
    provenance = {
        key: value
        for key, value in {
            "oracle_storage_key": oracle_storage_key,
            "run_id": run_id,
            "trace_id": trace_id,
        }.items()
        if value is not None
    }
    return SkillOptScoreProjection(
        case_id=case_id,
        soft=round(optimization_score / 100.0, 4),
        hard=1 if optimizer_complete_answer(quality_scorecard) else 0,
        breakdown={
            "quality_scorecard": quality_scorecard.model_dump(mode="json"),
            "evaluation_stage": evaluation_stage,
            "release_eligible": release_eligible,
            "provenance": provenance,
        },
    )


def project_eval_trace(payload: Mapping[str, Any]) -> SkillOptTraceProjection:
    """Project one eval trace without copying tool results into optimization inputs."""

    metadata = _mapping(payload.get("metadata"))
    return SkillOptTraceProjection(
        status=str(payload.get("status") or "unknown"),
        answer=str(payload.get("answer") or ""),
        run_id=_optional_text(payload.get("run_id")),
        trace_id=_optional_text(payload.get("langfuse_trace_id")),
        metadata={
            key: deepcopy(metadata[key])
            for key in (
                "answer_review",
                "evidence_ledger",
                "stop_gate",
                "run_signals",
                "behavior_snapshot",
            )
            if key in metadata
        },
        tool_call_chain=[_project_tool_call(record) for record in _tool_call_records(metadata)],
    )


def build_skillopt_evaluation_artifact(
    *,
    case_id: str,
    quality_scorecard: QualityScorecard,
    trace: Mapping[str, Any],
    evaluation_stage: Literal["provisional", "accepted"],
    release_eligible: bool,
    oracle_storage_key: str | None = None,
) -> SkillOptEvaluationArtifact:
    """Bind one projected trace to the score calculated for that same case."""

    projected_trace = project_eval_trace(trace)
    return SkillOptEvaluationArtifact(
        score=project_quality_scorecard(
            case_id=case_id,
            quality_scorecard=quality_scorecard,
            evaluation_stage=evaluation_stage,
            release_eligible=release_eligible,
            oracle_storage_key=oracle_storage_key,
            run_id=projected_trace.run_id,
            trace_id=projected_trace.trace_id,
        ),
        trace=projected_trace,
    )


def skillopt_evaluator_result(artifact: SkillOptEvaluationArtifact) -> dict[str, Any]:
    """Adapt to SkillOpt's evaluate result without its legacy threshold or judge."""
    return {
        "id": artifact.score.case_id,
        "hard": artifact.score.hard,
        "soft": artifact.score.soft,
        "vlm_ceiling": False,
        "breakdown": deepcopy(artifact.score.breakdown),
        "failure_reasons": (
            []
            if artifact.trace.status == "completed"
            else [f"run status '{artifact.trace.status}', not completed"]
        ),
    }


def canonical_artifact_bytes(artifact: SkillOptEvaluationArtifact) -> bytes:
    """Serialize an artifact reproducibly for a file-level bundle digest."""

    return json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_skillopt_evaluation_artifact(payload: bytes) -> SkillOptEvaluationArtifact:
    """Load the bounded I1 artifact without invoking an optimizer or evaluator."""

    return SkillOptEvaluationArtifact.model_validate_json(payload)


def _tool_call_records(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = metadata.get("tool_call_records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, Mapping)]


def _project_tool_call(record: Mapping[str, Any]) -> dict[str, object]:
    arguments = _mapping(record.get("arguments"))
    error = record.get("error")
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or record.get("tool") or ""),
        "arguments": deepcopy(arguments),
        "status": str(record.get("status") or "unknown"),
        "error": deepcopy(error) if isinstance(error, Mapping) else None,
    }


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
