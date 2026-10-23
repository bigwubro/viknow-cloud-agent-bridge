"""Development eval service for running inspect tools without Agent runtime."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from loguru import logger
from pydantic import BaseModel, Field

from viknow.core.config import EvalJudgeModelConfig, ModelProvider, ToolConfigs
from viknow.core.tools import ViKnowToolHandlers
from viknow.inspect_trace import (
    InspectTraceCollector,
    activate_inspect_trace_collector,
    reset_inspect_trace_collector,
)

SUPPORTED_INSPECT_TOOLS = {
    "document_inspect",
    "image_inspect",
    "video_inspect",
    "video_inspect_2_0",
    "visual_ground",
}


class JudgeMetric(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = ""


class InspectJudgeOutput(BaseModel):
    visual_factuality: JudgeMetric
    question_relevance: JudgeMetric
    localization_quality: JudgeMetric
    coarse_coverage_quality: JudgeMetric
    refinement_information_gain: JudgeMetric
    refinement_regression_safety: JudgeMetric
    final_claim_utility: JudgeMetric
    overall_score: int = Field(ge=1, le=5)
    workflow_recommendation: str
    summary: str = ""


@dataclass(frozen=True)
class InspectToolEvalCase:
    case_id: str
    tool: str
    arguments: dict[str, Any]
    metadata: dict[str, Any] | None = None


class InspectToolJudge(Protocol):
    def evaluate(self, case: InspectToolEvalCase, run: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ConfiguredInspectToolJudge:
    chat_model: Any
    repo_root: Path
    model: str
    max_tokens: int
    enable_thinking: bool

    def evaluate(self, case: InspectToolEvalCase, run: dict[str, Any]) -> dict[str, Any]:
        from viknow.core.evidence import Evidence

        resolved = run["resolved_evidence"]
        evidence = (
            [Evidence.model_validate(item) for item in resolved]
            if isinstance(resolved, list)
            else Evidence.model_validate(resolved)
        )
        message = self._build_message(case, evidence, run)
        response = self.chat_model.bind(
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens,
            extra_body={"enable_thinking": self.enable_thinking},
        ).invoke([message])
        result = _parse_judge_json_response(response)
        if result is None:
            return {
                "status": "judge_failed",
                "model": self.model,
                "error": "judge returned no valid JSON output",
            }
        return {
            "status": "completed",
            "model": self.model,
            **result.model_dump(mode="json"),
        }

    def _build_message(self, case: InspectToolEvalCase, evidence: Any, run: dict[str, Any]) -> Any:
        from langchain_core.messages import HumanMessage

        content: list[dict[str, Any]] = [{"type": "text", "text": _judge_prompt(case, run)}]
        content.extend(_judge_media_content(case.tool, evidence, self.repo_root))
        return HumanMessage(content=content)


@dataclass
class InspectToolEvalService:
    handlers: Any | None = None
    judge: InspectToolJudge | None = None
    judge_timeout_seconds: float | None = None
    repo_root: Path | None = None
    tool_configs: ToolConfigs | None = None
    knowledge_adapter: object | None = None
    filesystem_manager: object | None = None

    def run_case(self, case: InspectToolEvalCase) -> dict[str, Any]:
        handlers = self.handlers or self._build_handlers()
        trace_collector = InspectTraceCollector()
        base = {
            "case_id": case.case_id,
            "tool": case.tool,
            "arguments": case.arguments,
            "metadata": case.metadata or {},
            "phase": "not_started",
            "evidence_resolved": False,
            "resolved_evidence": None,
            "inspect_trace": trace_collector.entries,
            "tool_result": None,
            "judge_result": None,
            "telemetry": {},
            "rule_checks": [],
            "risk_triage": {},
            "status": "pending",
            "error": None,
        }
        resolve_started = perf_counter()
        evidence = self._resolve_evidence(case, handlers)
        resolve_elapsed_seconds = round(perf_counter() - resolve_started, 3)
        run = {
            **base,
            "phase": "inspect_tool",
            "evidence_resolved": True,
            "resolved_evidence": _model_dump_json(evidence),
            "telemetry": {"resolve_elapsed_seconds": resolve_elapsed_seconds},
        }
        token = activate_inspect_trace_collector(trace_collector)
        try:
            inspect_started = perf_counter()
            tool_arguments = case.arguments
            if case.tool == "visual_ground" and "evidence" not in tool_arguments:
                tool_arguments = {
                    **case.arguments,
                    "evidence": _model_dump_json(evidence),
                }
            raw_result = _tool_handler(case.tool, handlers)(tool_arguments)
            inspect_elapsed_seconds = round(perf_counter() - inspect_started, 3)
            tool_result = _json_object_or_raw(raw_result)
        finally:
            reset_inspect_trace_collector(token)
        run = {
            **run,
            "phase": "completed",
            "status": "completed",
            "tool_result": tool_result,
            "telemetry": {
                **run["telemetry"],
                "inspect_elapsed_seconds": inspect_elapsed_seconds,
            },
        }
        run = self._run_judge(case, run)
        return enrich_inspect_tool_eval_run({**run, "phase": "completed"})

    def judge_case(self, case: InspectToolEvalCase, run: dict[str, Any]) -> dict[str, Any]:
        if self.judge is None:
            return {
                **run,
                "judge_result": None,
                "telemetry": _as_dict(run.get("telemetry")),
            }
        return self._run_judge(case, run)

    def _run_judge(
        self,
        case: InspectToolEvalCase,
        run: dict[str, Any],
    ) -> dict[str, Any]:
        if self.judge is None:
            return run
        if self.judge_timeout_seconds is not None and self.judge_timeout_seconds > 0:
            return self._run_judge_with_timeout(case, run)
        try:
            judge_started = perf_counter()
            judge_result = self.judge.evaluate(case, run)
            judge_elapsed_seconds = round(perf_counter() - judge_started, 3)
            return {
                **run,
                "judge_result": judge_result,
                "telemetry": {
                    **_as_dict(run.get("telemetry")),
                    "judge_elapsed_seconds": judge_elapsed_seconds,
                },
            }
        except Exception as exc:
            return {
                **run,
                "judge_result": {"status": "judge_failed", "error": str(exc)},
            }

    def _run_judge_with_timeout(
        self,
        case: InspectToolEvalCase,
        run: dict[str, Any],
    ) -> dict[str, Any]:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        judge_started = perf_counter()
        future = executor.submit(self.judge.evaluate, case, run)  # type: ignore[union-attr]
        try:
            judge_result = future.result(timeout=self.judge_timeout_seconds)
            judge_elapsed_seconds = round(perf_counter() - judge_started, 3)
        except concurrent.futures.TimeoutError:
            judge_elapsed_seconds = round(perf_counter() - judge_started, 3)
            executor.shutdown(wait=False, cancel_futures=True)
            return {
                **run,
                "judge_result": {
                    "status": "judge_failed",
                    "error": f"judge timed out after {self.judge_timeout_seconds:g} seconds",
                },
                "telemetry": {
                    **_as_dict(run.get("telemetry")),
                    "judge_elapsed_seconds": judge_elapsed_seconds,
                },
            }
        except Exception as exc:
            judge_elapsed_seconds = round(perf_counter() - judge_started, 3)
            executor.shutdown(wait=False, cancel_futures=True)
            return {
                **run,
                "judge_result": {"status": "judge_failed", "error": str(exc)},
                "telemetry": {
                    **_as_dict(run.get("telemetry")),
                    "judge_elapsed_seconds": judge_elapsed_seconds,
                },
            }
        executor.shutdown(wait=True)
        return {
            **run,
            "judge_result": judge_result,
            "telemetry": {
                **_as_dict(run.get("telemetry")),
                "judge_elapsed_seconds": judge_elapsed_seconds,
            },
        }

    def _build_handlers(self) -> ViKnowToolHandlers:
        return ViKnowToolHandlers(
            repo_root=self.repo_root,
            tool_configs=self.tool_configs,
            knowledge_adapter=self.knowledge_adapter,  # type: ignore[arg-type]
            filesystem_manager=self.filesystem_manager,
        )

    def _resolve_evidence(self, case: InspectToolEvalCase, handlers: Any) -> Any:
        if case.tool in {"document_inspect", "image_inspect"}:
            return handlers.multimodal._resolve_chunk_evidence_or_parse(
                case.arguments,
                tool_name=case.tool,
            )
        if case.tool == "video_inspect":
            from viknow.core.tools.multimodal import (
                AdvancedVideoInspectClipLocator,
                VideoInspectLocator,
            )

            video_path = case.arguments.get("video_path")
            if video_path and hasattr(handlers.multimodal, "_video_inspect_evidence_from_path"):
                return handlers.multimodal._video_inspect_evidence_from_path(
                    AdvancedVideoInspectClipLocator.model_validate(
                        {
                            "library_id": case.arguments.get("library_id"),
                            "asset_id": case.arguments.get("asset_id"),
                            "time_range": case.arguments.get("time_range"),
                            "evidence_id": case.arguments.get("evidence_id"),
                            "title": case.arguments.get("title"),
                            "video_path": video_path,
                        }
                    )
                )
            locator = VideoInspectLocator.model_validate(
                {
                    "library_id": case.arguments.get("library_id"),
                    "asset_id": case.arguments.get("asset_id"),
                    "time_range": case.arguments.get("time_range"),
                    "evidence_id": case.arguments.get("evidence_id"),
                    "title": case.arguments.get("title"),
                }
            )
            return handlers.multimodal._video_inspect_evidence_from_locator(locator)
        if case.tool == "video_inspect_2_0":
            from viknow.core.tools.multimodal import (
                AdvancedVideoInspectClipLocator,
                VideoInspectLocator,
            )

            advanced = AdvancedVideoInspectClipLocator.model_validate(
                {
                    key: case.arguments.get(key)
                    for key in (
                        "library_id",
                        "asset_id",
                        "evidence_id",
                        "title",
                        "time_range",
                        "fps",
                        "crop",
                    )
                    if case.arguments.get(key) is not None
                }
            )
            locator = VideoInspectLocator.model_validate(
                advanced.model_dump(
                    include={"library_id", "asset_id", "time_range", "evidence_id", "title"}
                )
            )
            return handlers.multimodal._video_inspect_evidence_from_locator(locator)
        if case.tool == "visual_ground":
            if "evidence" in case.arguments or case.arguments.get("evidence_id"):
                return handlers.multimodal._resolve_chunk_evidence_or_parse(
                    case.arguments,
                    tool_name="visual_ground",
                )
            if case.arguments.get("library_id") and case.arguments.get("asset_id"):
                from viknow.core.tools.multimodal import (
                    AdvancedVideoInspectClipLocator,
                    VideoInspectLocator,
                )

                video_path = case.arguments.get("video_path")
                if video_path and hasattr(handlers.multimodal, "_video_inspect_evidence_from_path"):
                    return handlers.multimodal._video_inspect_evidence_from_path(
                        AdvancedVideoInspectClipLocator.model_validate(
                            {
                                "library_id": case.arguments.get("library_id"),
                                "asset_id": case.arguments.get("asset_id"),
                                "time_range": case.arguments.get("time_range"),
                                "evidence_id": case.arguments.get("evidence_id"),
                                "title": case.arguments.get("title"),
                                "video_path": video_path,
                            }
                        )
                    )
                locator = VideoInspectLocator.model_validate(
                    {
                        "library_id": case.arguments.get("library_id"),
                        "asset_id": case.arguments.get("asset_id"),
                        "time_range": case.arguments.get("time_range"),
                        "evidence_id": case.arguments.get("evidence_id"),
                        "title": case.arguments.get("title"),
                    }
                )
                return handlers.multimodal._video_inspect_evidence_from_locator(locator)
            return handlers.multimodal._resolve_chunk_evidence_or_parse(
                case.arguments,
                tool_name="visual_ground",
            )
        raise ValueError(
            f"unsupported inspect tool {case.tool!r}; expected one of "
            f"{sorted(SUPPORTED_INSPECT_TOOLS)}"
        )


def _tool_handler(tool: str, handlers: Any):
    if tool == "document_inspect":
        return handlers.document_inspect
    if tool == "image_inspect":
        return handlers.image_inspect
    if tool == "video_inspect":
        return handlers.video_inspect
    if tool == "video_inspect_2_0":
        return handlers.video_inspect_2_0
    if tool == "visual_ground":
        return handlers.locate_anything
    raise ValueError(
        f"unsupported inspect tool {tool!r}; expected one of {sorted(SUPPORTED_INSPECT_TOOLS)}"
    )


def enrich_inspect_tool_eval_run(run: dict[str, Any]) -> dict[str, Any]:
    """Attach single-case telemetry, rule checks, and risk triage to an eval run."""

    telemetry = {
        **_stage_telemetry(run.get("inspect_trace")),
        **_advanced_visual_telemetry(run.get("tool_result"), run.get("inspect_trace")),
        **_as_dict(run.get("telemetry")),
        "token_usage": _run_token_usage(run),
        "tool_success": _tool_result_success(run.get("tool_result")),
        "final_claim_count": len(_final_claims(run.get("tool_result"))),
    }
    rule_checks = _rule_checks(run)
    return {
        **run,
        "telemetry": telemetry,
        "rule_checks": rule_checks,
        "risk_triage": _risk_triage(run, rule_checks),
    }


def _advanced_visual_telemetry(tool_result: object, trace: object) -> dict[str, Any]:
    if not isinstance(tool_result, dict) or tool_result.get("tool") != "video_inspect_2_0":
        return {}
    result = tool_result.get("result")
    video = result.get("video") if isinstance(result, dict) else None
    refine_calls = 0
    if isinstance(trace, list):
        refine_calls = sum(
            1 for item in trace if isinstance(item, dict) and item.get("stage") == "refine"
        )
    return {
        "visual_input_bytes": int(video.get("submitted_bytes") or 0)
        if isinstance(video, dict)
        else 0,
        "completed_clip_count": int(isinstance(video, dict) and video.get("status") == "completed"),
        "failed_clip_count": int(isinstance(video, dict) and video.get("status") == "failed"),
        "estimated_vlm_call_count": 1 + refine_calls,
    }


def _stage_telemetry(inspect_trace: object) -> dict[str, Any]:
    if not isinstance(inspect_trace, list):
        return {
            "stage_elapsed_seconds": {},
            "stage_token_usage": {},
            "total_stage_elapsed_seconds": 0.0,
            "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    stage_elapsed: dict[str, float] = {}
    stage_token_usage: dict[str, dict[str, int]] = {}
    total_token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for entry in inspect_trace:
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        payload = entry.get("payload")
        if not isinstance(stage, str) or not isinstance(payload, dict):
            continue
        elapsed = _optional_float(
            payload.get("elapsed_seconds", payload.get("stage_elapsed_seconds"))
        )
        if elapsed is not None:
            stage_elapsed[stage] = elapsed
        token_usage = _token_usage(payload.get("token_usage"))
        if _has_token_usage(token_usage):
            stage_token_usage[stage] = token_usage
            _add_token_usage(total_token_usage, token_usage)
    return {
        "stage_elapsed_seconds": stage_elapsed,
        "stage_token_usage": stage_token_usage,
        "total_stage_elapsed_seconds": round(sum(stage_elapsed.values()), 3),
        "token_usage": total_token_usage,
    }


def _rule_checks(run: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    claims = _final_claims(run.get("tool_result"))
    checks.append(_check("final_claims_present", bool(claims), count=len(claims)))
    checks.extend(_claim_identity_checks(claims))
    checks.extend(_claim_render_checks(claims))
    checks.extend(_video_timestamp_checks(run, claims))
    return checks


def _claim_identity_checks(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing_claim_ids = [
        index for index, claim in enumerate(claims, start=1) if not claim.get("claim_id")
    ]
    return [
        _check(
            "claim_ids_present",
            not missing_claim_ids,
            missing_claim_indexes=missing_claim_ids,
        )
    ]


def _claim_render_checks(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing_source = [
        index
        for index, claim in enumerate(claims, start=1)
        if not isinstance(claim.get("source"), dict)
    ]
    missing_render = [
        index
        for index, claim in enumerate(claims, start=1)
        if not isinstance(claim.get("render"), dict)
    ]
    return [
        _check("claim_sources_present", not missing_source, missing_claim_indexes=missing_source),
        _check("claim_renders_present", not missing_render, missing_claim_indexes=missing_render),
    ]


def _video_timestamp_checks(
    run: dict[str, Any],
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if run.get("tool") == "video_inspect_2_0":
        return _advanced_video_source_checks(run, claims)
    if run.get("tool") != "video_inspect":
        return []
    clip_range = _clip_range(run.get("resolved_evidence"))
    checks: list[dict[str, Any]] = []
    out_of_clip: list[int] = []
    zero_length: list[int] = []
    boundary_points: list[int] = []
    timestamp_keys: dict[tuple[float, float], list[int]] = {}
    for index, claim in enumerate(claims, start=1):
        start = _optional_float(claim.get("timestamp_start"))
        end = _optional_float(claim.get("timestamp_end"))
        if start is None or end is None:
            out_of_clip.append(index)
            continue
        if end <= start:
            zero_length.append(index)
        timestamp_keys.setdefault((start, end), []).append(index)
        if clip_range is not None:
            clip_start, clip_end = clip_range
            if start < clip_start or end > clip_end:
                out_of_clip.append(index)
            if start == clip_start or end == clip_end:
                boundary_points.append(index)
    duplicate_zero_length_groups = [
        indexes
        for (start, end), indexes in timestamp_keys.items()
        if start == end and len(indexes) > 1
    ]
    if clip_range is not None:
        checks.append(
            _check(
                "video_timestamps_in_clip",
                not out_of_clip,
                clip_start_seconds=clip_range[0],
                clip_end_seconds=clip_range[1],
                failing_claim_indexes=out_of_clip,
            )
        )
    checks.append(
        _check(
            "video_timestamps_non_zero_length",
            not zero_length,
            failing_claim_indexes=zero_length,
        )
    )
    checks.append(
        _check(
            "video_timestamps_not_duplicate_zero_length",
            not duplicate_zero_length_groups,
            duplicate_claim_index_groups=duplicate_zero_length_groups,
        )
    )
    checks.append(
        _check(
            "video_timestamps_not_all_on_clip_boundary",
            not boundary_points or len(boundary_points) < len(claims),
            boundary_claim_indexes=boundary_points,
        )
    )
    return checks


def _advanced_video_source_checks(
    run: dict[str, Any], claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    resolved = run.get("resolved_evidence")
    evidences = (
        [resolved]
        if isinstance(resolved, dict)
        else [item for item in resolved if isinstance(item, dict)]
        if isinstance(resolved, list)
        else []
    )
    ranges: dict[tuple[str, str], tuple[float, float]] = {}
    for evidence in evidences:
        if not isinstance(evidence, dict):
            continue
        metadata = evidence.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        key = (str(evidence.get("id") or ""), str(metadata.get("asset_id") or ""))
        clip_range = _clip_range(evidence)
        if clip_range is not None:
            ranges[key] = clip_range

    missing_sources: list[int] = []
    invalid_sources: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(claims, start=1):
        sources = claim.get("sources")
        if not isinstance(sources, list) or not sources:
            missing_sources.append(claim_index)
            continue
        for source_index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                invalid_sources.append({"claim": claim_index, "source": source_index})
                continue
            key = (str(source.get("evidence_id") or ""), str(source.get("asset_id") or ""))
            clip_range = ranges.get(key)
            start = _optional_float(source.get("timestamp_start"))
            end = _optional_float(source.get("timestamp_end"))
            valid = start is not None and end is not None and end > start
            if valid and clip_range is not None:
                valid = start >= clip_range[0] and end <= clip_range[1]
            if not valid or clip_range is None:
                invalid_sources.append({"claim": claim_index, "source": source_index})
    return [
        _check(
            "advanced_claim_sources_present",
            not missing_sources,
            missing_claim_indexes=missing_sources,
        ),
        _check(
            "advanced_source_timestamps_within_clip",
            not invalid_sources,
            invalid_sources=invalid_sources,
        ),
    ]


def _risk_triage(run: dict[str, Any], rule_checks: list[dict[str, Any]]) -> dict[str, Any]:
    risk_reasons: list[str] = []
    for check in rule_checks:
        if check.get("passed") is False:
            risk_reasons.append(str(check.get("name") or "rule_check_failed"))
    judge_result = run.get("judge_result")
    if isinstance(judge_result, dict) and judge_result.get("status") == "judge_failed":
        risk_reasons.append("judge_failed")
    if run.get("status") not in {"completed", "dry_run"}:
        risk_reasons.append(str(run.get("status") or "run_not_completed"))
    score = min(1.0, round(len(set(risk_reasons)) / 5, 3))
    return {
        "needs_human_review": bool(risk_reasons),
        "needs_human_review_score": score,
        "risk_reasons": sorted(set(risk_reasons)),
    }


def _check(name: str, passed: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, **details}


def _model_dump_json(value: Any) -> Any:
    if isinstance(value, list):
        return [_model_dump_json(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _json_object_or_raw(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
    return value


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _run_token_usage(run: dict[str, Any]) -> dict[str, int]:
    telemetry = _stage_telemetry(run.get("inspect_trace"))
    return _token_usage(telemetry.get("token_usage"))


def _add_token_usage(target: dict[str, int], source: object) -> None:
    source = _token_usage(source)
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        target[field] += source[field]


def _token_usage(value: object) -> dict[str, int]:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not isinstance(value, dict):
        return usage
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token_count = value.get(field)
        if isinstance(token_count, int) and not isinstance(token_count, bool):
            usage[field] = token_count
    return usage


def _has_token_usage(usage: dict[str, int]) -> bool:
    return any(value > 0 for value in usage.values())


def _tool_result_success(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    inner = value.get("result")
    if isinstance(inner, dict):
        return inner.get("success") is True
    return value.get("success") is True


def _final_claims(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    result = value.get("result")
    source = result if isinstance(result, dict) else value
    claims = source.get("claims") if isinstance(source, dict) else None
    if not isinstance(claims, list):
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def _clip_range(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("parts")
    if not isinstance(parts, list):
        return None
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "video":
            continue
        time_range = part.get("time_range")
        if not isinstance(time_range, dict):
            continue
        start = _optional_float(time_range.get("start_seconds"))
        end = _optional_float(time_range.get("end_seconds"))
        if start is not None and end is not None:
            return (start, end)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _parse_judge_json_response(response: Any) -> InspectJudgeOutput | None:
    raw_content = getattr(response, "content", response)
    if not isinstance(raw_content, str):
        raw_content = str(raw_content)
    try:
        obj = json.loads(raw_content)
    except json.JSONDecodeError:
        obj = _repair_json_object(raw_content)
    if not isinstance(obj, dict):
        logger.error("[inspect_tool_eval_judge] judge output is not a JSON object")
        return None
    try:
        return InspectJudgeOutput.model_validate(obj)
    except Exception as exc:
        logger.error(f"[inspect_tool_eval_judge] judge output schema validation failed: {exc}")
        return None


def _repair_json_object(raw_content: str) -> object | None:
    try:
        from json_repair import repair_json
    except ImportError:
        logger.error("[inspect_tool_eval_judge] json_repair not installed")
        return None
    try:
        clean = re.sub(
            r"<think>.*?</think>",
            "",
            raw_content,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        return repair_json(clean, return_objects=True)
    except Exception as exc:
        logger.error(f"[inspect_tool_eval_judge] json_repair failed: {exc}")
        return None


def build_inspect_tool_judge(
    judge_model: EvalJudgeModelConfig,
    *,
    repo_root: Path,
) -> ConfiguredInspectToolJudge:
    if judge_model.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported inspect eval judge provider: {judge_model.provider}")
    base_url = judge_model.resolve_base_url()
    if not base_url:
        raise RuntimeError("inspect eval judge base_url or base_url_env is required")
    model = judge_model.require_model()

    from langchain_openai import ChatOpenAI

    return ConfiguredInspectToolJudge(
        chat_model=ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=judge_model.require_api_key(),
            temperature=judge_model.temperature,
            timeout=judge_model.timeout_seconds,
            max_retries=0,
        ),
        repo_root=repo_root.resolve(),
        model=model,
        max_tokens=judge_model.max_tokens,
        enable_thinking=judge_model.enable_thinking,
    )


def _judge_prompt(case: InspectToolEvalCase, run: dict[str, Any]) -> str:
    output_schema = {
        "visual_factuality": {"score": "integer 1-5", "reason": "string"},
        "question_relevance": {"score": "integer 1-5", "reason": "string"},
        "localization_quality": {"score": "integer 1-5", "reason": "string"},
        "coarse_coverage_quality": {"score": "integer 1-5", "reason": "string"},
        "refinement_information_gain": {"score": "integer 1-5", "reason": "string"},
        "refinement_regression_safety": {"score": "integer 1-5", "reason": "string"},
        "final_claim_utility": {"score": "integer 1-5", "reason": "string"},
        "overall_score": "integer 1-5",
        "workflow_recommendation": "keep | simplify | needs_more_data",
        "summary": "string",
    }
    payload = {
        "case_id": case.case_id,
        "tool": case.tool,
        "arguments": case.arguments,
        "tool_result": run.get("tool_result"),
        "inspect_trace": run.get("inspect_trace"),
    }
    return "\n".join(
        [
            "你是 ViKnow inspect 工具的评估模型。请直接查看随消息提供的图片或视频片段，",
            "并基于视觉输入、工具问题、stage trace 和最终 claims 进行结构化评分。",
            "必须只输出一个 JSON 对象，且只能包含指定 schema 字段。不要回显 case、arguments、"
            "tool_result、inspect_trace 或媒体内容。不要输出 Markdown。",
            "",
            "评分规则：",
            "- visual_factuality: claims 是否被视觉内容直接支持。",
            "- question_relevance: claims 是否回答工具问题。",
            "- localization_quality: 图片来源或视频时间定位是否合理。",
            "- coarse_coverage_quality: coarse 观察是否覆盖了画面中与问题相关的主要事实。",
            "- refinement_information_gain: 如果触发 refinement，它是否补充了明确的新信息；"
            "未触发且 coarse 已充分时应给高分。",
            "- refinement_regression_safety: refinement 是否避免丢失、曲解或覆盖 coarse 事实；"
            "分数越高表示越安全。",
            "- final_claim_utility: final claims 是否适合 Agent 引用。",
            "- overall_score: 1 到 5 的综合分。",
            "- workflow_recommendation: 输出 keep、simplify、needs_more_data 三者之一。",
            "",
            "只能基于可见视觉内容评分。caption、ASR、OCR 或 trace 只能作为辅助上下文。",
            "输出 schema：",
            json.dumps(output_schema, ensure_ascii=False),
            "待评估数据如下：",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def _judge_media_content(tool: str, evidence: Any, repo_root: Path) -> list[dict[str, Any]]:
    from viknow.core.evidence import EvidencePartKind
    from viknow.core.multimodal.evidence_utils import resolve_media_path
    from viknow.core.multimodal.review import review_video_path

    evidences = evidence if isinstance(evidence, list) else [evidence]
    content: list[dict[str, Any]] = []
    if tool == "image_inspect":
        for part in evidences[0].parts:
            if part.kind != EvidencePartKind.IMAGE or not part.uri:
                continue
            image_path = resolve_media_path(repo_root, part.uri)
            mime_type = part.mime_type or "image/jpeg"
            image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                }
            )
            return content
    if tool in {"video_inspect", "video_inspect_2_0"}:
        for index, item in enumerate(evidences, start=1):
            for part in item.parts:
                if part.kind != EvidencePartKind.VIDEO or not part.uri:
                    continue
                video_path = resolve_media_path(repo_root, part.uri)
                time_range = None
                if part.time_range is not None:
                    time_range = (part.time_range.start_seconds, part.time_range.end_seconds)
                with review_video_path(video_path, time_range) as clip_path:
                    video_b64 = base64.b64encode(clip_path.read_bytes()).decode("ascii")
                content.append({"type": "text", "text": f"[judge source clip-{index}]"})
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{video_b64}"},
                    }
                )
                break
    return content
