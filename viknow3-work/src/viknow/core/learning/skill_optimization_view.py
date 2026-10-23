"""Read-only, bounded projection of one SkillOpt experiment for the admin UI."""

from __future__ import annotations

import difflib
import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.learning.case_review import SkillOptimizationExperiment, SkillOptimizationTask

_MAX_JSON_BYTES = 5_000_000
_MAX_SKILL_BYTES = 1_000_000
_MAX_SKILL_VERSIONS = 50
_MAX_STEPS = 200
_MAX_EDITS = 50
_MAX_TOOL_CALLS = 200
_SENSITIVE_KEYS = ("api_key", "apikey", "authorization", "password", "secret", "token")


class SkillVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    characters: int = Field(ge=0)
    content: str


class SkillDiffLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    text: str


class SkillDiffView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before_label: str
    after_label: str
    added: int = Field(ge=0)
    removed: int = Field(ge=0)
    lines: list[SkillDiffLine]


class SkillEditView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    op: str
    content: str | None = None
    source_type: str | None = None
    support_count: int | None = None


class SkillOptimizationStepView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=1)
    action: str | None = None
    rollout_hard: float | None = None
    rollout_soft: float | None = None
    selection_hard: float | None = None
    selection_soft: float | None = None
    candidate_gate_score: float | None = None
    wall_time_seconds: float | None = None
    reasoning: str | None = None
    edits: list[SkillEditView] = Field(default_factory=list)


class ToolCallView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: str
    arguments_summary: str
    error: str | None = None


class SkillOptimizationStageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    hard: float | None = None
    soft: float | None = None
    answer: str
    judge_reason: str
    failure_reasons: list[str] = Field(default_factory=list)
    tool_call_chain: list[ToolCallView] = Field(default_factory=list)


class SkillOptimizationSampleView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    question: str
    before: SkillOptimizationStageView
    after: SkillOptimizationStageView
    delta_hard: float | None = None
    delta_soft: float | None = None


class SkillOptimizationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str | None
    available: bool
    skill_versions: list[SkillVersionView] = Field(default_factory=list)
    skill_diff: SkillDiffView | None = None
    steps: list[SkillOptimizationStepView] = Field(default_factory=list)
    sample: SkillOptimizationSampleView | None = None


class SkillOptimizationProgressEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sequence: int
    stage: str
    elapsed_seconds: float | None = None
    case_id: str | None = None
    status: str | None = None
    candidate_sha256: str | None = None
    soft: float | None = None
    hard: float | None = None
    wall_time_seconds: float | None = None
    run_id: str | None = None
    trace_id: str | None = None
    log_index: str | None = None
    score_delta: float | None = None


class SkillOptimizationExperimentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    artifact_id: str | None = None
    available: bool = False
    skill_versions: list[SkillVersionView] = Field(default_factory=list)
    skill_diff: SkillDiffView | None = None
    steps: list[SkillOptimizationStepView] = Field(default_factory=list)
    samples: list[SkillOptimizationSampleView] = Field(default_factory=list)
    logs: list[SkillOptimizationProgressEvent] = Field(default_factory=list)
    token_summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    runtime: dict[str, float | int] = Field(default_factory=dict)


def load_skill_optimization_experiment_view(
    *, experiment_root: Path | None, experiment: SkillOptimizationExperiment
) -> SkillOptimizationExperimentView:
    empty = SkillOptimizationExperimentView(
        experiment_id=experiment.id, artifact_id=experiment.artifact_id
    )
    if experiment.artifact_id is None or experiment_root is None:
        return empty
    experiment_dir = _experiment_dir(experiment_root, experiment.artifact_id)
    output = experiment_dir / "skillopt-output"
    if not _is_bounded(output, experiment_dir) or not output.is_dir():
        return empty
    versions = _skill_versions(output, experiment_dir)
    steps = _steps(output, experiment_dir)
    runtime = _read_json(output / "runtime_state.json", experiment_dir)
    summary = _read_json(output / "summary.json", experiment_dir)
    token_summary = summary.get("token_summary") or {}
    numeric_usage = (
        {
            str(stage): {
                str(name): value
                for name, value in usage.items()
                if type(value) is int and value >= 0
            }
            for stage, usage in token_summary.items()
            if isinstance(usage, dict)
        }
        if isinstance(token_summary, dict)
        else {}
    )
    return SkillOptimizationExperimentView(
        experiment_id=experiment.id,
        artifact_id=experiment.artifact_id,
        available=True,
        skill_versions=versions,
        skill_diff=_skill_diff(versions, selected_best=True),
        steps=steps,
        samples=[
            sample
            for case in experiment.cases
            if (sample := _sample(output, case.run_id, steps, experiment_dir)) is not None
        ],
        logs=_progress_events(output / "progress.jsonl", experiment_dir),
        token_summary=numeric_usage,
        runtime={
            key: value
            for key in ("last_completed_step", "best_score", "best_step", "current_score")
            if (value := _number(runtime.get(key))) is not None
        },
    )


def _progress_events(path: Path, boundary: Path) -> list[SkillOptimizationProgressEvent]:
    from pydantic import ValidationError

    if not _is_bounded(path, boundary):
        return []
    try:
        with path.open("rb") as stream:
            start = max(0, path.stat().st_size - _MAX_JSON_BYTES)
            stream.seek(start)
            if start:
                stream.readline()
            lines = deque(stream, maxlen=1000)
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(SkillOptimizationProgressEvent.model_validate_json(line))
        except (ValueError, ValidationError):
            continue  # A live JSONL writer may not have completed its last line yet.
    return events


def load_skill_optimization_view(
    *, experiment_root: Path, task: SkillOptimizationTask
) -> SkillOptimizationView:
    """Project only the artifacts owned by ``task.experiment_id`` under the configured root."""
    if task.experiment_id is None:
        return SkillOptimizationView(experiment_id=None, available=False)
    experiment_dir = _experiment_dir(experiment_root, task.experiment_id)
    output_dir = experiment_dir / "skillopt-output"
    if not _is_bounded(output_dir, experiment_dir) or not output_dir.is_dir():
        return SkillOptimizationView(experiment_id=task.experiment_id, available=False)

    versions = _skill_versions(output_dir, experiment_dir)
    steps = _steps(output_dir, experiment_dir)
    return SkillOptimizationView(
        experiment_id=task.experiment_id,
        available=True,
        skill_versions=versions,
        skill_diff=_skill_diff(versions),
        steps=steps,
        sample=_sample(output_dir, task.source_run_id, steps, experiment_dir),
    )


def _experiment_dir(root: Path, experiment_id: str) -> Path:
    if not experiment_id or Path(experiment_id).name != experiment_id:
        raise ValueError("invalid SkillOpt experiment_id")
    resolved_root = root.resolve()
    resolved = (resolved_root / experiment_id).resolve()
    if resolved.parent != resolved_root:
        raise ValueError("invalid SkillOpt experiment_id")
    return resolved


def _is_bounded(path: Path, boundary: Path) -> bool:
    try:
        return path.resolve().is_relative_to(boundary.resolve())
    except OSError:
        return False


def _read_json(path: Path, boundary: Path) -> dict[str, Any]:
    if not _is_bounded(path, boundary):
        return {}
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_text(path: Path, boundary: Path) -> str | None:
    if not _is_bounded(path, boundary):
        return None
    try:
        if path.stat().st_size > _MAX_SKILL_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _skill_versions(output_dir: Path, boundary: Path) -> list[SkillVersionView]:
    versions: list[SkillVersionView] = []
    skills_dir = output_dir / "skills"
    try:
        paths = sorted(
            (
                path
                for path in skills_dir.glob("skill_v*.md")
                if re.fullmatch(r"skill_v\d+\.md", path.name)
            ),
            key=lambda path: int(path.stem.removeprefix("skill_v")),
        )
        if len(paths) > _MAX_SKILL_VERSIONS:
            paths = paths[:1] + paths[-(_MAX_SKILL_VERSIONS - 1) :]
    except OSError:
        paths = []
    for path in paths:
        match = re.fullmatch(r"skill_v(\d+)\.md", path.name)
        content = _read_text(path, boundary)
        if match is not None and content is not None:
            versions.append(
                SkillVersionView(
                    label=f"v{int(match.group(1))}", characters=len(content), content=content
                )
            )
    best = _read_text(output_dir / "best_skill.md", boundary)
    if best is not None:
        versions.append(SkillVersionView(label="best", characters=len(best), content=best))
    return versions


def _skill_diff(
    versions: list[SkillVersionView], *, selected_best: bool = False
) -> SkillDiffView | None:
    numbered = [version for version in versions if version.label != "best"]
    best = next((version for version in versions if version.label == "best"), None)
    if not numbered or (len(numbered) < 2 and not (selected_best and best)):
        return None
    before = numbered[0]
    after = best if selected_best and best else numbered[-1]
    lines: list[SkillDiffLine] = []
    added = removed = 0
    for line in difflib.ndiff(before.content.splitlines(), after.content.splitlines()):
        prefix, text = line[:2], line[2:]
        if prefix == "+ ":
            lines.append(SkillDiffLine(kind="add", text=text))
            added += 1
        elif prefix == "- ":
            lines.append(SkillDiffLine(kind="remove", text=text))
            removed += 1
        elif prefix == "  ":
            lines.append(SkillDiffLine(kind="context", text=text))
    return SkillDiffView(
        before_label=before.label,
        after_label=after.label,
        added=added,
        removed=removed,
        lines=lines,
    )


def _steps(output_dir: Path, boundary: Path) -> list[SkillOptimizationStepView]:
    result = []
    paths = sorted(
        (
            path
            for path in (output_dir / "steps").glob("step_*/step_record.json")
            if re.fullmatch(r"step_\d+", path.parent.name)
        ),
        key=lambda path: int(path.parent.name.removeprefix("step_")),
    )
    for record_path in paths[-_MAX_STEPS:]:
        record = _read_json(record_path, boundary)
        step = record.get("step")
        if not isinstance(step, int) or step < 1:
            continue
        ranked = _read_json(record_path.with_name("ranked_edits.json"), boundary)
        edits = ranked.get("edits")
        result.append(
            SkillOptimizationStepView(
                step=step,
                action=_string(record.get("action")),
                rollout_hard=_number(record.get("rollout_hard")),
                rollout_soft=_number(record.get("rollout_soft")),
                selection_hard=_number(record.get("selection_hard")),
                selection_soft=_number(record.get("selection_soft")),
                candidate_gate_score=_number(record.get("candidate_gate_score")),
                wall_time_seconds=_number(record.get("wall_time_s")),
                reasoning=_text_value(ranked.get("reasoning"), limit=20_000),
                edits=[_edit(item) for item in edits[:_MAX_EDITS] if isinstance(item, dict)]
                if isinstance(edits, list)
                else [],
            )
        )
    return result


def _sample(
    output_dir: Path,
    case_id: str,
    steps: list[SkillOptimizationStepView],
    boundary: Path,
) -> SkillOptimizationSampleView | None:
    # Final test artifacts belong to the trainer-selected best version, even when
    # its last optimization step was rejected or skipped.
    before_root = output_dir / "test_eval_baseline" / "predictions" / case_id
    after_root = output_dir / "test_eval" / "predictions" / case_id
    if not (after_root / "scored.json").is_file():
        if not steps:
            return None
        before_root = output_dir / "selection_eval_baseline" / "predictions" / case_id
        after_root = (
            output_dir
            / "steps"
            / f"step_{steps[-1].step:04d}"
            / "selection_eval"
            / "predictions"
            / case_id
        )
    before = _stage(before_root, boundary)
    after = _stage(after_root, boundary)
    if before is None or after is None:
        return None
    before_trace = _read_json(before_root / "trace.json", boundary)
    after_trace = _read_json(after_root / "trace.json", boundary)
    return SkillOptimizationSampleView(
        case_id=case_id,
        question=str(before_trace.get("question") or after_trace.get("question") or ""),
        before=before,
        after=after,
        delta_hard=_delta(after.hard, before.hard),
        delta_soft=_delta(after.soft, before.soft),
    )


def _stage(root: Path, boundary: Path) -> SkillOptimizationStageView | None:
    trace = _read_json(root / "trace.json", boundary)
    scored = _read_json(root / "scored.json", boundary)
    if not trace and not scored:
        return None
    breakdown = scored.get("breakdown")
    failures = scored.get("failure_reasons")
    calls = trace.get("tool_call_chain")
    return SkillOptimizationStageView(
        status=str(trace.get("status") or scored.get("status") or "unknown"),
        hard=_number(scored.get("hard")),
        soft=_number(scored.get("soft")),
        answer=_text_value(trace.get("final_answer"), limit=200_000) or "",
        judge_reason=_text_value(breakdown.get("judge_reason"), limit=20_000) or ""
        if isinstance(breakdown, dict)
        else "",
        failure_reasons=[str(item) for item in failures] if isinstance(failures, list) else [],
        tool_call_chain=[
            _tool_call(item) for item in calls[:_MAX_TOOL_CALLS] if isinstance(item, dict)
        ]
        if isinstance(calls, list)
        else [],
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_value(value: Any, *, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _edit(value: dict[str, Any]) -> SkillEditView:
    support = value.get("support_count")
    return SkillEditView(
        op=str(value.get("op") or "unknown"),
        content=_text_value(value.get("content"), limit=20_000),
        source_type=_string(value.get("source_type")),
        support_count=support
        if isinstance(support, int) and not isinstance(support, bool)
        else None,
    )


def _tool_call(value: dict[str, Any]) -> ToolCallView:
    arguments = value.get("arguments")
    safe_arguments = _redact(arguments) if isinstance(arguments, dict) else {}
    summary = json.dumps(safe_arguments, ensure_ascii=False, sort_keys=True)
    return ToolCallView(
        name=str(value.get("name") or "unknown"),
        status=str(value.get("status") or "unknown"),
        arguments_summary=summary[:4_000],
        error=_text_value(value.get("error"), limit=2_000),
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if any(marker in str(key).lower() for marker in _SENSITIVE_KEYS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:2_000]
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:2_000]


def _delta(after: float | None, before: float | None) -> float | None:
    return after - before if after is not None and before is not None else None
