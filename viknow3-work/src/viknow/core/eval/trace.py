"""Project eval recorder memory and run events into API response shape."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.recorder import EvalModelCallTraceRecorder
from viknow.core.eval.run_signals import project_run_signals
from viknow.core.runtime.domain import AgentEvent, AgentEventType, KnowledgeAssetSelection
from viknow.core.session.domain import RunRecord
from viknow.core.tools.base import parse_tool_payload


class EvalPerformance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttft: float | None = Field(
        default=None,
        description="Seconds from eval request start to the first streamed token.",
    )
    total_cost: float | None = Field(
        default=None,
        description="Seconds from eval request start to the last streamed token.",
    )
    token_in: int | None = Field(
        default=None,
        description="Total LLM input tokens across the full Q&A flow.",
    )
    token_out: int | None = Field(
        default=None,
        description="Total LLM output tokens across the full Q&A flow.",
    )
    tool_call_count: int = Field(
        default=0,
        description="Total executed tool calls across the full Q&A flow.",
    )
    thinking_step: int = Field(
        default=0,
        description="Model rounds that planned tool calls or skill selection.",
    )
    model_spans: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-model-round latency and token usage; last entry is the final answer generation."
        ),
    )


class EvalMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activated_skill: str | None = None
    tool_calls: list[list[str]] = Field(default_factory=list)
    tool_call_records: list[dict[str, Any]] = Field(default_factory=list)
    query_refine_plans: list[dict[str, Any]] = Field(default_factory=list)
    search_result_summaries: list[dict[str, Any]] = Field(default_factory=list)
    inspect_result_summaries: list[dict[str, Any]] = Field(default_factory=list)
    citations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    selected_knowledge_assets: list[KnowledgeAssetSelection] = Field(default_factory=list)
    candidate_evidence_packages: list[dict[str, Any]] = Field(default_factory=list)
    research_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    answer_review: dict[str, Any] | None = None
    stop_gate: dict[str, Any] | None = None
    discover_ground_truth: dict[str, Any] | None = None
    run_signals: list[dict[str, Any]] = Field(default_factory=list)
    behavior_snapshot: dict[str, Any] | None = None


class EvalRunTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    answer: str | None = None
    answer_disposition: dict[str, str] | None = None
    performance: EvalPerformance
    context: list[Any] = Field(default_factory=list)
    metadata: EvalMetadata
    run_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    langfuse_trace_id: str | None = None
    langfuse_trace_name: str | None = None
    status: str | None = None
    error: str | None = None


def project_eval_trace(
    recorder: EvalModelCallTraceRecorder,
    events: list[AgentEvent],
    run: RunRecord,
    query: str,
    *,
    knowledge_adapter: Any | None = None,
) -> EvalRunTrace:
    answer: str | None = None
    answer_disposition: dict[str, str] | None = None
    for event in reversed(events):
        if event.type == AgentEventType.MESSAGE_COMPLETED and answer is None:
            text = event.payload.get("text")
            if isinstance(text, str) and text:
                answer = text
            raw_disposition = event.payload.get("answer_disposition")
            if isinstance(raw_disposition, dict):
                answer_disposition = {
                    "kind": str(raw_disposition.get("kind") or ""),
                    "reason": str(raw_disposition.get("reason") or ""),
                }

    performance_payload = recorder.build_performance_payload()
    token_in = _optional_int(performance_payload.get("token_in")) or 0
    token_out = _optional_int(performance_payload.get("token_out")) or 0
    if token_in == 0 and token_out == 0:
        event_in, event_out = _llm_tokens_from_events(events)
        if event_in is not None or event_out is not None:
            token_in = event_in or 0
            token_out = event_out or 0
    performance = EvalPerformance(
        ttft=_optional_float(performance_payload.get("ttft")),
        total_cost=_optional_float(performance_payload.get("total_cost")),
        token_in=token_in,
        token_out=token_out,
        tool_call_count=_count_executed_tool_calls(events),
        thinking_step=_optional_int(performance_payload.get("thinking_step")) or 0,
        model_spans=_model_spans_from_payload(performance_payload),
    )
    candidate_evidence_packages = _candidate_evidence_packages(events)
    inspect_tool_results = _inspect_tool_results(
        events,
        knowledge_adapter=knowledge_adapter,
    )
    metadata = EvalMetadata(
        activated_skill=performance_payload.get("activated_skill")
        if isinstance(performance_payload.get("activated_skill"), str)
        else None,
        tool_calls=_metadata_tool_calls(events, performance_payload.get("tool_calls")),
        tool_call_records=_metadata_tool_call_records(events),
        query_refine_plans=_query_refine_plans(events),
        search_result_summaries=_search_result_summaries(events),
        inspect_result_summaries=_inspect_result_summaries(inspect_tool_results, events),
        citations=_message_completed_citations(events, knowledge_adapter=knowledge_adapter),
        selected_knowledge_assets=_selected_knowledge_assets(events),
        candidate_evidence_packages=candidate_evidence_packages,
        research_outcomes=_research_outcomes(candidate_evidence_packages),
        answer_review=_final_answer_review(events),
        stop_gate=_stop_gate_snapshot(events),
        discover_ground_truth=_discover_ground_truth(knowledge_adapter),
        run_signals=[item.model_dump() for item in project_run_signals(events).signals]
        if events
        else [],
        behavior_snapshot=_behavior_snapshot(events),
    )

    return EvalRunTrace(
        query=query,
        answer=answer,
        answer_disposition=answer_disposition,
        performance=performance,
        context=_context_with_eval_messages(
            recorder.last_context_messages(),
            inspect_tool_results,
        ),
        metadata=metadata,
        run_id=run.id,
        session_id=run.session_id,
        user_id=run.user_id,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        error=run.error,
    )


def _count_executed_tool_calls(events: list[AgentEvent]) -> int:
    return sum(
        1
        for event in events
        if event.type in {AgentEventType.TOOL_CALL_COMPLETED, AgentEventType.TOOL_CALL_FAILED}
    )


def _llm_tokens_from_events(events: list[AgentEvent]) -> tuple[int | None, int | None]:
    for event in reversed(events):
        if event.type != AgentEventType.MESSAGE_COMPLETED:
            continue
        usage = event.payload.get("token_usage")
        if not isinstance(usage, dict):
            continue
        token_in = _usage_token_count(usage, "input_tokens", "prompt_tokens")
        token_out = _usage_token_count(usage, "output_tokens", "completion_tokens")
        if token_in is not None or token_out is not None:
            return token_in or 0, token_out or 0
    return None, None


def _model_spans_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("model_spans")
    if not isinstance(raw, list):
        return []
    spans: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized: dict[str, Any] = {}
        index = item.get("index")
        if isinstance(index, int):
            normalized["index"] = index
        latency = item.get("latency_seconds")
        if isinstance(latency, (int, float)):
            normalized["latency_seconds"] = float(latency)
        token_in = item.get("token_in")
        if isinstance(token_in, int):
            normalized["token_in"] = token_in
        token_out = item.get("token_out")
        if isinstance(token_out, int):
            normalized["token_out"] = token_out
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            normalized["tool_calls"] = [str(t) for t in tool_calls if str(t)]
        spans.append(normalized)
    return spans


def _usage_token_count(usage: dict[str, Any], primary: str, fallback: str) -> int | None:
    for key in (primary, fallback):
        value = usage.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _metadata_tool_calls(events: list[AgentEvent], recorder_tool_calls: object) -> list[list[str]]:
    executed = _executed_tool_calls_matrix(events)
    if executed:
        return executed
    return _tool_calls_matrix(recorder_tool_calls)


def _metadata_tool_call_records(events: list[AgentEvent]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in events:
        if event.type not in {AgentEventType.TOOL_CALL_COMPLETED, AgentEventType.TOOL_CALL_FAILED}:
            continue
        name = _event_tool_name(event)
        if not name:
            continue
        records.append(
            _without_empty(
                {
                    "id": str(event.payload.get("id") or ""),
                    "name": name,
                    "arguments": _event_tool_arguments(event.payload.get("arguments")),
                    "status": "failed"
                    if event.type == AgentEventType.TOOL_CALL_FAILED
                    else "completed",
                    "result_chars": _serialized_chars(event.payload.get("result")),
                }
            )
        )
    return records


def _query_refine_plans(events: list[AgentEvent]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for event in events:
        if (
            event.type != AgentEventType.TOOL_CALL_COMPLETED
            or _event_tool_name(event) != "query_refine"
        ):
            continue
        result = parse_tool_payload(event.payload.get("result"))
        if not result or result.get("error"):
            continue
        plans.append(
            _without_empty(
                {
                    "tool_call_id": str(event.payload.get("id") or ""),
                    "original_query": result.get("original_query"),
                    "coverage_requirement": result.get("coverage_requirement"),
                    "answer_slots": result.get("answer_slots") or [],
                    "retrieval_queries": result.get("retrieval_queries") or [],
                }
            )
        )
    return plans


def _search_result_summaries(events: list[AgentEvent]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        if (
            event.type != AgentEventType.TOOL_CALL_COMPLETED
            or _event_tool_name(event) != "knowledge_search"
        ):
            continue
        arguments = _event_tool_arguments(event.payload.get("arguments"))
        result = parse_tool_payload(event.payload.get("result"))
        evidence = result.get("evidence")
        candidates = evidence if isinstance(evidence, list) else []
        summaries.append(
            {
                "tool_call_id": str(event.payload.get("id") or ""),
                "library_id": str(arguments.get("library_id") or ""),
                "query": str(arguments.get("query") or ""),
                "candidate_count": len(candidates),
                "candidates": [
                    _search_candidate_locator(item, default_library_id=arguments.get("library_id"))
                    for item in candidates
                    if isinstance(item, dict)
                ],
            }
        )
    return summaries


def _search_candidate_locator(
    evidence: dict[str, Any], *, default_library_id: Any
) -> dict[str, Any]:
    metadata = evidence.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return _without_empty(
        {
            "id": evidence.get("id"),
            "evidence_id": evidence.get("evidence_id"),
            "chunk_id": evidence.get("chunk_id") or metadata.get("chunk_id"),
            "asset_id": evidence.get("asset_id") or metadata.get("asset_id"),
            "segment_id": evidence.get("segment_id") or metadata.get("segment_id"),
            "segment_ids": evidence.get("segment_ids") or metadata.get("segment_ids"),
            "library_id": evidence.get("library_id")
            or metadata.get("library_id")
            or default_library_id,
            "time_range": evidence.get("time_range") or metadata.get("time_range"),
        }
    )


def _candidate_evidence_packages(events: list[AgentEvent]) -> list[dict[str, Any]]:
    """Expose only validated Oracle output required for later human Gate B review."""

    from viknow.core.eval.candidate_evidence import CandidateEvidencePackage

    packages: list[dict[str, Any]] = []
    for event in events:
        if event.type != AgentEventType.TOOL_CALL_COMPLETED:
            continue
        if _event_tool_name(event) != "evidence_package":
            continue
        result = parse_tool_payload(event.payload.get("result"))
        if not isinstance(result, dict) or result.get("status") != "pending_gate_b":
            continue
        raw_package = result.get("package")
        if not isinstance(raw_package, dict):
            continue
        try:
            package = CandidateEvidencePackage.model_validate(raw_package)
        except ValueError:
            continue
        packages.append(package.model_dump(mode="json"))
    return packages


def _research_outcomes(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project coverage terminal states for review without treating them as facts."""

    outcomes: list[dict[str, Any]] = []
    for package in packages:
        outcome = package.get("research_outcome")
        task = package.get("task")
        if not isinstance(outcome, dict) or not isinstance(task, dict):
            continue
        status = outcome.get("status")
        coverage = outcome.get("coverage")
        if not isinstance(status, str) or not isinstance(coverage, list):
            continue
        outcomes.append(
            {
                "case_id": task.get("case_id"),
                "task_id": task.get("task_id"),
                "status": status,
                "coverage": coverage,
            }
        )
    return outcomes


def _final_answer_review(events: list[AgentEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.type != AgentEventType.MESSAGE_COMPLETED:
            continue
        review = event.payload.get("answer_review")
        if isinstance(review, dict):
            return dict(review)
    return None


def _event_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    payload = _json_object(value)
    return payload or {}


def _context_with_eval_messages(
    context: list[Any],
    inspect_results: list[dict[str, Any]],
) -> list[Any]:
    if not inspect_results:
        return context
    inspect_results_by_id = {
        item["call_id"]: item["eval_message"]
        for item in inspect_results
        if item.get("call_id") and item.get("eval_message")
    }
    inspect_results_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in inspect_results:
        name = str(item.get("name") or "")
        eval_message = item.get("eval_message")
        if name and isinstance(eval_message, dict):
            inspect_results_by_name.setdefault(name, []).append(eval_message)
    context_tool_names = _context_tool_call_names(context)
    enriched: list[Any] = []
    for message in context:
        tool_call_id = _context_tool_call_id(message)
        eval_message = inspect_results_by_id.get(tool_call_id)
        if eval_message is None:
            tool_name = context_tool_names.get(tool_call_id) or _context_tool_result_name(message)
            if tool_name in {
                "text_inspect",
                "image_inspect",
                "document_inspect",
                "video_inspect",
                "video_inspect_2_0",
                "evidence_asr",
                "evidence_ocr",
            }:
                queue = inspect_results_by_name.get(tool_name) or []
                if queue:
                    eval_message = queue.pop(0)
        if eval_message is None:
            enriched.append(message)
            continue
        if isinstance(message, dict):
            enriched.append({**message, "eval_message": eval_message})
            continue
        if hasattr(message, "model_dump"):
            payload = message.model_dump(mode="json")
            if isinstance(payload, dict):
                payload["eval_message"] = eval_message
                enriched.append(payload)
                continue
        enriched.append(message)
    return enriched


def _inspect_result_summaries(
    inspect_results: list[dict[str, Any]],
    events: list[AgentEvent],
) -> list[dict[str, Any]]:
    arguments_by_call_id = {
        str(event.payload.get("id") or ""): _event_tool_arguments(event.payload.get("arguments"))
        for event in events
        if event.type == AgentEventType.TOOL_CALL_COMPLETED and str(event.payload.get("id") or "")
    }
    summaries: list[dict[str, Any]] = []
    for item in inspect_results:
        call_id = str(item.get("call_id") or "")
        eval_message = item.get("eval_message")
        if not call_id or not isinstance(eval_message, dict):
            continue
        locator = {
            key: value
            for key in ("library_id", "asset_id", "evidence_id", "question", "time_range")
            if (value := arguments_by_call_id.get(call_id, {}).get(key)) not in (None, "", {})
        }
        summary: dict[str, Any] = {
            "tool_call_id": call_id,
            "tool": str(eval_message.get("tool") or item.get("name") or ""),
            "locator": locator,
            "success": eval_message.get("success"),
            "claims": [
                _bounded_inspect_claim(claim)
                for claim in eval_message.get("claims") or []
                if isinstance(claim, dict)
            ],
        }
        source = _bounded_inspect_source(eval_message.get("source"))
        if source:
            summary["source"] = source
        if eval_message.get("error") not in (None, "", {}):
            summary["error"] = eval_message.get("error")
        summaries.append(_without_empty(summary))
    return summaries


def _bounded_inspect_claim(claim: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: claim.get(key)
        for key in (
            "claim",
            "claim_id",
            "agent_citation",
            "citation_index",
            "display_label",
            "confidence",
            "quote_verified",
            "verify_reason",
        )
        if claim.get(key) not in (None, "", [], {})
    }
    source = _bounded_inspect_source(claim.get("source"))
    if source:
        summary["source"] = source
    return summary


def _bounded_inspect_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key in (
            "document_id",
            "asset_id",
            "chunk_id",
            "segment_id",
            "source_image_id",
            "time_range",
            "absolute_time_range",
        )
        if (item := value.get(key)) not in (None, "", [], {})
    }


def _context_tool_call_id(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id") or message.get("id") or "").strip()
    return str(getattr(message, "tool_call_id", None) or getattr(message, "id", None) or "").strip()


def _context_tool_call_names(context: list[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in context:
        tool_calls = []
        if isinstance(message, dict):
            value = message.get("tool_calls")
            tool_calls = value if isinstance(value, list) else []
        else:
            value = getattr(message, "tool_calls", None)
            tool_calls = value if isinstance(value, list) else []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
            function = tool_call.get("function")
            name = ""
            if isinstance(function, dict):
                name = str(function.get("name") or "").strip()
            if not name:
                name = str(tool_call.get("name") or "").strip()
            if call_id and name:
                names[call_id] = name
    return names


def _context_tool_result_name(message: Any) -> str:
    content = None
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    payload = _json_object(content)
    if payload is None:
        return ""
    return str(payload.get("tool") or "").strip()


def _inspect_tool_results(
    events: list[AgentEvent],
    *,
    knowledge_adapter: Any | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in events:
        if event.type != AgentEventType.TOOL_CALL_COMPLETED:
            continue
        name = _event_tool_name(event)
        if name not in {
            "text_inspect",
            "image_inspect",
            "document_inspect",
            "video_inspect",
            "video_inspect_2_0",
            "evidence_asr",
            "evidence_ocr",
        }:
            continue
        call_id = str(event.payload.get("id") or "").strip()
        if not call_id:
            continue
        payload = _json_object(event.payload.get("debug_result")) or _json_object(
            event.payload.get("result")
        )
        eval_message = _eval_inspect_message(
            name,
            payload,
            knowledge_adapter=knowledge_adapter,
        )
        if eval_message is not None:
            results.append(
                {
                    "call_id": call_id,
                    "name": name,
                    "eval_message": eval_message,
                }
            )
    return results


def _eval_inspect_message(
    tool_name: str,
    payload: dict[str, Any] | None,
    *,
    knowledge_adapter: Any | None = None,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    from viknow.core.tools.base import unwrap_tool_data

    payload = unwrap_tool_data(payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    claims = result.get("claims")
    if not isinstance(claims, list):
        claims = []
    evidence = payload.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    message_source = _eval_inspect_message_source(
        tool_name,
        evidence,
        knowledge_adapter=knowledge_adapter,
    )
    eval_message: dict[str, Any] = {
        "tool": str(payload.get("tool") or tool_name),
        "success": result.get("success"),
        "claims": [
            _eval_inspect_claim(
                tool_name,
                claim,
                evidence=evidence,
                knowledge_adapter=knowledge_adapter,
            )
            for claim in claims
            if isinstance(claim, dict)
        ],
    }
    if message_source:
        eval_message["source"] = message_source
    if result.get("error"):
        eval_message["error"] = result.get("error")
    return _without_empty(eval_message)


def _eval_inspect_message_source(
    tool_name: str,
    evidence: dict[str, Any],
    *,
    knowledge_adapter: Any | None = None,
) -> dict[str, Any] | None:
    if tool_name not in {
        "image_inspect",
        "document_inspect",
        "video_inspect",
        "video_inspect_2_0",
        "evidence_asr",
        "evidence_ocr",
    }:
        return None
    source = _source_from_evidence(tool_name, evidence)
    if not source:
        return None
    return _compact_citation_source(
        str(source.get("type") or ""),
        source,
        knowledge_adapter=knowledge_adapter,
    )


def _eval_inspect_claim(
    tool_name: str,
    claim: dict[str, Any],
    *,
    evidence: dict[str, Any],
    knowledge_adapter: Any | None = None,
) -> dict[str, Any]:
    source = claim.get("source")
    source = source if isinstance(source, dict) else {}
    if (
        tool_name
        not in {
            "image_inspect",
            "document_inspect",
            "video_inspect",
            "video_inspect_2_0",
            "evidence_asr",
            "evidence_ocr",
        }
        or source
    ):
        source = _source_with_evidence_defaults(tool_name, source, evidence)
    source_type = str(source.get("type") or "")
    payload = {
        "claim": claim.get("claim"),
        "claim_id": claim.get("claim_id"),
        "agent_citation": claim.get("agent_citation"),
        "citation_index": claim.get("citation_index"),
        "display_label": claim.get("display_label"),
        "confidence": claim.get("confidence"),
        "quote_verified": claim.get("quote_verified"),
        "verify_reason": claim.get("verify_reason"),
    }
    if source:
        payload["source"] = _compact_citation_source(
            source_type,
            source,
            knowledge_adapter=knowledge_adapter,
        )
    return _without_empty(payload)


def _source_with_evidence_defaults(
    tool_name: str,
    source: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if not evidence:
        return source
    fallback = _source_from_evidence(tool_name, evidence)
    if not fallback:
        return source
    merged = {**fallback, **source}
    fallback_metadata = fallback.get("metadata")
    source_metadata = source.get("metadata")
    if isinstance(fallback_metadata, dict) or isinstance(source_metadata, dict):
        merged["metadata"] = {
            **(fallback_metadata if isinstance(fallback_metadata, dict) else {}),
            **(source_metadata if isinstance(source_metadata, dict) else {}),
        }
    return merged


def _source_from_evidence(tool_name: str, evidence: dict[str, Any]) -> dict[str, Any]:
    metadata = evidence.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    parts = evidence.get("parts")
    parts = parts if isinstance(parts, list) else []
    image_part = _first_evidence_part(parts, "image")
    video_part = _first_evidence_part(parts, "video")
    text_part = _first_evidence_part(parts, "text")
    if tool_name in {"document_inspect", "image_inspect"} or image_part is not None:
        source_type = "image"
        media_part = image_part or {}
    elif tool_name in {"video_inspect", "video_inspect_2_0"} or video_part is not None:
        source_type = "video"
        media_part = video_part or {}
    else:
        source_type = "text"
        media_part = {}

    chunk_content = metadata.get("chunk_content")
    if not chunk_content and isinstance(text_part, dict):
        chunk_content = text_part.get("text")

    uri = media_part.get("uri") if isinstance(media_part, dict) else None
    source_image_id = metadata.get("source_image_id")
    if source_type == "image" and not source_image_id and uri:
        source_image_id = Path(str(uri)).name

    return _without_empty(
        {
            "type": source_type,
            "library_id": metadata.get("library_id"),
            "asset_id": metadata.get("asset_id") or metadata.get("full_doc_id"),
            "evidence_id": evidence.get("id") or metadata.get("chunk_id"),
            "title": evidence.get("title"),
            "source_image_id": source_image_id,
            "uri": uri,
            "metadata": {
                **metadata,
                "chunk_id": metadata.get("chunk_id") or evidence.get("id"),
                "chunk_content": chunk_content,
            },
        }
    )


def _first_evidence_part(parts: list[Any], kind: str) -> dict[str, Any] | None:
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_kind = part.get("kind")
        if str(part_kind.value if hasattr(part_kind, "value") else part_kind) == kind:
            return part
    return None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _message_completed_citations(
    events: list[AgentEvent],
    *,
    knowledge_adapter: Any | None = None,
) -> dict[str, dict[str, Any]]:
    for event in reversed(events):
        if event.type != AgentEventType.MESSAGE_COMPLETED:
            continue
        citations = event.payload.get("citations")
        if not isinstance(citations, dict):
            continue
        return {
            str(key): _compact_citation(value, knowledge_adapter=knowledge_adapter)
            for key, value in citations.items()
            if isinstance(value, dict)
        }
    return {}


def _stop_gate_snapshot(events: list[AgentEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.type != AgentEventType.STOP_GATE_EVALUATED:
            continue
        payload = event.payload
        return {
            "evaluated": True,
            "blocked": bool(payload.get("blocked")),
            "reason_code": str(payload.get("reason_code") or ""),
            "pre_gate_marker_count": _optional_int(payload.get("pre_gate_marker_count")) or 0,
            "pre_gate_resolved_citation_count": (
                _optional_int(payload.get("pre_gate_resolved_citation_count")) or 0
            ),
            "citation_policy": str(payload.get("citation_policy") or ""),
        }
    return None


def _selected_knowledge_assets(events: list[AgentEvent]) -> list[KnowledgeAssetSelection]:
    for event in events:
        if event.type != AgentEventType.RUN_STARTED:
            continue
        selected = event.payload.get("selected_knowledge_assets")
        if not isinstance(selected, list):
            return []
        return [KnowledgeAssetSelection.model_validate(asset) for asset in selected]
    return []


def _behavior_snapshot(events: list[AgentEvent]) -> dict[str, Any] | None:
    for event in events:
        if event.type != AgentEventType.RUN_STARTED:
            continue
        snapshot = event.payload.get("behavior_snapshot")
        return dict(snapshot) if isinstance(snapshot, dict) else None
    return None


def _compact_citation(
    citation: dict[str, Any],
    *,
    knowledge_adapter: Any | None = None,
) -> dict[str, Any]:
    source = citation.get("source")
    source = source if isinstance(source, dict) else {}
    source_type = str(source.get("type") or "")
    compact: dict[str, Any] = {
        "marker": str(citation.get("marker") or citation.get("agent_citation") or ""),
        "type": source_type,
        "tool": str(citation.get("tool") or ""),
        "claim": str(citation.get("claim") or ""),
        "display_label": str(citation.get("display_label") or ""),
        "confidence": citation.get("confidence"),
        "source": _compact_citation_source(
            source_type,
            source,
            knowledge_adapter=knowledge_adapter,
        ),
    }
    if citation.get("quote_verified") is not None:
        compact["quote_verified"] = citation.get("quote_verified")
    return compact


def _compact_citation_source(
    source_type: str,
    source: dict[str, Any],
    *,
    knowledge_adapter: Any | None = None,
) -> dict[str, Any]:
    metadata = source.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    common = _citation_common_source_fields(source, metadata)
    if source_type == "video":
        return _without_empty(
            {
                **common,
                "video_name": source.get("title") or source.get("asset_id"),
                "time_range": _time_range(
                    source.get("timestamp_start"),
                    source.get("timestamp_end"),
                ),
                "absolute_time_range": _time_range(
                    metadata.get("absolute_timestamp_start"),
                    metadata.get("absolute_timestamp_end"),
                ),
            }
        )
    if source_type == "image":
        return _without_empty(
            {
                **common,
                "source_image_id": source.get("source_image_id"),
                "image_data_url": _image_data_url(source, metadata, knowledge_adapter),
            }
        )
    return _without_empty(
        {
            **common,
            "segments": _text_segments(source, metadata),
        }
    )


def _citation_common_source_fields(
    source: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    document_id = source.get("asset_id") or metadata.get("full_doc_id") or source.get("title")
    chunk_id = source.get("evidence_id") or metadata.get("chunk_id") or source.get("segment_id")
    return _without_empty(
        {
            "document_id": document_id,
            "title": source.get("title"),
            "chunk_id": chunk_id,
            "chunk_content": metadata.get("chunk_content"),
        }
    )


def _text_segments(source: dict[str, Any], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    segment_ids = _string_values(
        metadata.get("verified_source_segments"),
        metadata.get("source_segments"),
        source.get("segment_id"),
    )
    anchor_text = _text_anchor_content_by_segment(source.get("anchors"))
    span_text = _evidence_span_content_by_segment(metadata.get("evidence_spans"))
    if not segment_ids:
        segment_ids = list(anchor_text) or list(span_text)

    segments: list[dict[str, Any]] = []
    for segment_id in segment_ids:
        segment_content = (
            anchor_text.get(segment_id)
            or span_text.get(segment_id)
            or (
                metadata.get("segment_content")
                if str(source.get("segment_id") or "") == segment_id
                else None
            )
        )
        segments.append(
            _without_empty(
                {
                    "segment_id": segment_id,
                    "segment_content": segment_content,
                }
            )
        )
    return segments


def _text_anchor_content_by_segment(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    content: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        segment_id = str(item.get("segment_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if segment_id and text:
            content[segment_id] = text
    return content


def _evidence_span_content_by_segment(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    content: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        segment_id = str(item.get("segment_id") or "").strip()
        text = str(item.get("supporting_quote") or "").strip()
        if segment_id and text:
            content[segment_id] = text
    return content


def _string_values(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        candidates = value if isinstance(value, list | tuple) else [value]
        for candidate in candidates:
            item = str(candidate or "").strip()
            if item and item not in result:
                result.append(item)
    return result


def _time_range(start: Any, end: Any) -> dict[str, float] | None:
    start_value = _optional_float(start)
    end_value = _optional_float(end)
    if start_value is None or end_value is None:
        return None
    return {"start_seconds": start_value, "end_seconds": end_value}


def _image_data_url(
    source: dict[str, Any],
    metadata: dict[str, Any],
    knowledge_adapter: Any | None,
) -> str | None:
    path = _image_path(source, metadata, knowledge_adapter)
    if path is None or not path.is_file():
        return None
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _image_path(
    source: dict[str, Any],
    metadata: dict[str, Any],
    knowledge_adapter: Any | None,
) -> Path | None:
    uri = str(source.get("uri") or "").strip()
    if uri:
        uri_path = Path(uri)
        if uri_path.is_absolute() and uri_path.is_file():
            return uri_path
    if knowledge_adapter is None or not hasattr(knowledge_adapter, "resolve_asset_image_path"):
        return None
    library_id = str(source.get("library_id") or "").strip()
    asset_id = str(source.get("asset_id") or metadata.get("full_doc_id") or "").strip()
    image_name = Path(str(source.get("source_image_id") or uri or "")).name
    if not library_id or not asset_id or not image_name:
        return None
    try:
        resolved = knowledge_adapter.resolve_asset_image_path(
            library_id=library_id,
            asset_id=asset_id,
            image_name=image_name,
        )
    except Exception:
        return None
    return Path(resolved) if resolved is not None else None


def _discover_ground_truth(knowledge_adapter: Any | None) -> dict[str, Any] | None:
    if knowledge_adapter is None or not hasattr(knowledge_adapter, "list_libraries"):
        return None
    try:
        libraries = knowledge_adapter.list_libraries()
    except Exception as exc:
        return {"error": str(exc), "libraries": []}
    return {
        "libraries": [
            library.model_dump(mode="json") if hasattr(library, "model_dump") else dict(library)
            for library in libraries
        ]
    }


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "")}


def _serialized_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _executed_tool_calls_matrix(events: list[AgentEvent]) -> list[list[str]]:
    """Group executed tool names by model-call round, matching the web UI timeline."""
    has_model_calls = any(event.type == AgentEventType.MODEL_CALL_STARTED for event in events)
    matrix: list[list[str]] = []
    current_round: list[str] = []
    for event in events:
        if event.type == AgentEventType.MODEL_CALL_STARTED:
            if current_round:
                matrix.append(current_round)
            current_round = []
            continue
        if event.type != AgentEventType.TOOL_CALL_STARTED:
            continue
        name = _event_tool_name(event)
        if name is None:
            continue
        if has_model_calls:
            current_round.append(name)
        else:
            matrix.append([name])
    if current_round:
        matrix.append(current_round)
    return matrix


def _event_tool_name(event: AgentEvent) -> str | None:
    name = event.payload.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _tool_calls_matrix(value: object) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    matrix: list[list[str]] = []
    for row in value:
        if not isinstance(row, list):
            continue
        matrix.append([str(item) for item in row if isinstance(item, str)])
    return matrix


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
