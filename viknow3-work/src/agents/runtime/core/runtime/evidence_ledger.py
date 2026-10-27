"""Project one run's durable events into a compact, runtime-owned evidence ledger."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.runtime.core.answer_review.claim_bundle import CLAIM_TOOL_NAMES, collect_claim_bundle
from agents.runtime.core.runtime.domain import AgentEvent, AgentEventType, ToolCallRecord

_CANDIDATE_TOOL_NAMES = frozenset({"knowledge_search", "list_video_segments", "list_doc_segments"})


class ToolActionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceClaimStatus(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"


class ExecutionIncidentCategory(StrEnum):
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    VALIDATION_ERROR = "validation_error"
    PARSE_ERROR = "parse_error"
    INTERNAL_ERROR = "internal_error"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ToolActionFact(BaseModel):
    tool_call_id: str
    tool_name: str
    status: ToolActionStatus
    started_event_sequence: int | None = None
    terminal_event_sequence: int | None = None
    arguments_sha256: str = ""
    result_sha256: str | None = None
    result_chars: int | None = None
    error_sha256: str | None = None
    retry_index: int = 1


class EvidenceLocatorFact(BaseModel):
    """A content-free location for one candidate, claim, or citation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    library_id: str = ""
    asset_id: str = ""
    modality: str = ""
    evidence_id: str = ""
    segment_id: str = ""
    time_range: dict[str, float] | None = None


class CandidateManifestFact(BaseModel):
    tool_call_id: str
    tool_name: str
    source_event_sequence: int
    result_sha256: str
    result_chars: int
    status: str = "candidate"
    locators: list[EvidenceLocatorFact] = Field(default_factory=list)


class EvidenceClaimFact(BaseModel):
    claim_ref: str
    tool_call_id: str
    tool_name: str
    source_event_sequence: int
    status: EvidenceClaimStatus
    claim: str
    source: dict[str, Any] = Field(default_factory=dict)
    supports: list[dict[str, Any]] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)
    sampling_range: dict[str, float] | None = None
    claim_id: str = ""
    citation_index: int | None = None
    eligibility_reason: str = ""
    locator: EvidenceLocatorFact | None = None


class FinalCitationFact(BaseModel):
    source_event_sequence: int
    marker: str
    claim_ref: str


class StopGateFact(BaseModel):
    source_event_sequence: int
    blocked: bool
    reason_code: str = ""


class ToolGateFact(BaseModel):
    source_event_sequence: int
    tool_name: str
    blocked: bool
    reason_code: str = ""


class AnswerReviewFact(BaseModel):
    source_event_sequence: int
    attempt: int
    max_attempts: int
    passed: bool
    retryable: bool
    overall_score: int | None = None
    risk_kinds: list[str] = Field(default_factory=list)


class ExecutionIncidentFact(BaseModel):
    source_event_sequence: int
    category: ExecutionIncidentCategory
    tool_call_id: str | None = None
    tool_name: str | None = None
    error_sha256: str | None = None
    error_code: str | None = None
    error_kind: str | None = None
    retryable: bool | None = None


class ContextCompactionFact(BaseModel):
    source_event_sequence: int
    cutoff_index: int | None = None


class RunEvidenceLedger(BaseModel):
    """Current-run facts maintained exclusively from runtime events."""

    schema_version: int = 2
    answer_slots: list[dict[str, Any]] = Field(default_factory=list)
    slot_evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    policy_version: str = "h2-observe-only"
    tool_actions: list[ToolActionFact] = Field(default_factory=list)
    candidate_manifests: list[CandidateManifestFact] = Field(default_factory=list)
    claims: list[EvidenceClaimFact] = Field(default_factory=list)
    final_citations: list[FinalCitationFact] = Field(default_factory=list)
    stop_gates: list[StopGateFact] = Field(default_factory=list)
    tool_gates: list[ToolGateFact] = Field(default_factory=list)
    reviews: list[AnswerReviewFact] = Field(default_factory=list)
    incidents: list[ExecutionIncidentFact] = Field(default_factory=list)
    compactions: list[ContextCompactionFact] = Field(default_factory=list)

    def apply(self, event: AgentEvent) -> None:
        """Apply one durable runtime event without copying raw large payloads."""

        if event.type == AgentEventType.TOOL_CALL_STARTED:
            self._record_tool_started(event)
        elif event.type == AgentEventType.TOOL_CALL_COMPLETED:
            self._record_tool_completed(event)
        elif event.type == AgentEventType.TOOL_CALL_FAILED:
            self._record_tool_failed(event)
        elif event.type == AgentEventType.STOP_GATE_EVALUATED:
            self.stop_gates.append(
                StopGateFact(
                    source_event_sequence=event.sequence,
                    blocked=bool(event.payload.get("blocked")),
                    reason_code=str(event.payload.get("reason_code") or ""),
                )
            )
        elif event.type == AgentEventType.TOOL_GATE_EVALUATED:
            self.tool_gates.append(
                ToolGateFact(
                    source_event_sequence=event.sequence,
                    tool_name=str(event.payload.get("tool_name") or ""),
                    blocked=bool(event.payload.get("blocked")),
                    reason_code=str(event.payload.get("reason_code") or ""),
                )
            )
        elif event.type == AgentEventType.MESSAGE_COMPLETED:
            self._record_final_citations(event)
        elif event.type == AgentEventType.ANSWER_REVIEW_COMPLETED:
            self._record_review(event)
        elif event.type == AgentEventType.RUN_BUDGET_EXHAUSTED:
            self.incidents.append(
                ExecutionIncidentFact(
                    source_event_sequence=event.sequence,
                    category=ExecutionIncidentCategory.BUDGET_EXHAUSTED,
                )
            )
        elif event.type == AgentEventType.CONTEXT_COMPACTED:
            cutoff_index = event.payload.get("cutoff_index")
            self.compactions.append(
                ContextCompactionFact(
                    source_event_sequence=event.sequence,
                    cutoff_index=cutoff_index if isinstance(cutoff_index, int) else None,
                )
            )

    def _record_tool_started(self, event: AgentEvent) -> None:
        tool_call_id = str(event.payload.get("id") or "")
        if not tool_call_id or self._action(tool_call_id) is not None:
            return
        tool_name = str(event.payload.get("name") or "")
        retry_index = 1 + sum(action.tool_name == tool_name for action in self.tool_actions)
        self.tool_actions.append(
            ToolActionFact(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolActionStatus.RUNNING,
                started_event_sequence=event.sequence,
                arguments_sha256=_digest(event.payload.get("arguments")),
                retry_index=retry_index,
            )
        )

    def _record_tool_completed(self, event: AgentEvent) -> None:
        action = self._action_for_terminal_event(event)
        result = event.payload.get("result")
        action.status = ToolActionStatus.COMPLETED
        action.terminal_event_sequence = event.sequence
        action.result_sha256 = _digest(result)
        action.result_chars = _serialized_chars(result)
        if error := _tool_result_error(result):
            self.incidents.append(
                ExecutionIncidentFact(
                    source_event_sequence=event.sequence,
                    category=_TOOL_ERROR_KIND_TO_CATEGORY[error["kind"]],
                    tool_call_id=action.tool_call_id,
                    tool_name=action.tool_name,
                    error_sha256=_digest(error["raw"]),
                    error_code=error["code"],
                    error_kind=error["kind"],
                    retryable=error["retryable"],
                )
            )
        if action.tool_name in _CANDIDATE_TOOL_NAMES:
            self.candidate_manifests.append(
                CandidateManifestFact(
                    tool_call_id=action.tool_call_id,
                    tool_name=action.tool_name,
                    source_event_sequence=event.sequence,
                    result_sha256=action.result_sha256,
                    result_chars=action.result_chars,
                    locators=_candidate_locators(result),
                )
            )
        if action.tool_name == "query_refine" and not self.answer_slots:
            from agents.runtime.core.tools.base import unwrap_tool_data
            from agents.runtime.core.tools.knowledge import QueryRefineAnswerSlot

            payload = unwrap_tool_data(_json_object(result) or {})
            for slot in payload.get("answer_slots") or []:
                try:
                    validated = QueryRefineAnswerSlot.model_validate(slot)
                except (ValueError, TypeError):
                    continue
                self.answer_slots.append(validated.model_dump(mode="json"))
        self._record_claims(event, action)

    def _record_tool_failed(self, event: AgentEvent) -> None:
        action = self._action_for_terminal_event(event)
        error = event.payload.get("error")
        action.status = ToolActionStatus.FAILED
        action.terminal_event_sequence = event.sequence
        action.error_sha256 = _digest(error)
        self.incidents.append(
            ExecutionIncidentFact(
                source_event_sequence=event.sequence,
                category=_incident_category(error),
                tool_call_id=action.tool_call_id,
                tool_name=action.tool_name,
                error_sha256=action.error_sha256,
            )
        )

    def _record_claims(self, event: AgentEvent, action: ToolActionFact) -> None:
        if action.tool_name not in CLAIM_TOOL_NAMES:
            return
        record = ToolCallRecord(
            id=action.tool_call_id,
            name=action.tool_name,
            arguments="",
            result=_serialized_text(event.payload.get("result")),
        )
        all_claims = collect_claim_bundle([record], filter_invalid=False)
        eligible_keys = {
            _claim_key(item) for item in collect_claim_bundle([record], filter_invalid=True)
        }
        for item in all_claims:
            claim_key = _claim_key(item)
            claim_id = str(item.get("claim_id") or "")
            citation_index = item.get("citation_index")
            if not isinstance(citation_index, int):
                citation_index = None
            claim_ref = _claim_ref(event.run_id, action.tool_call_id, claim_id, citation_index)
            if not claim_ref:
                continue
            verified = claim_key in eligible_keys
            self.claims.append(
                EvidenceClaimFact(
                    claim_ref=claim_ref,
                    tool_call_id=action.tool_call_id,
                    tool_name=action.tool_name,
                    source_event_sequence=event.sequence,
                    status=(
                        EvidenceClaimStatus.VERIFIED if verified else EvidenceClaimStatus.INVALID
                    ),
                    claim=_bounded_text(item.get("claim")),
                    source=_compact_claim_source(item.get("source") or {}),
                    supports=list(item.get("supports") or []),
                    modalities=_claim_modalities(item, action.tool_name),
                    sampling_range=_time_range(
                        (_json_object(event.payload.get("arguments")) or {}).get("time_range")
                    ),
                    claim_id=claim_id,
                    citation_index=citation_index,
                    eligibility_reason=(
                        str(item.get("verify_reason") or "")
                        if verified
                        else "claim_did_not_meet_tool_verification_rule"
                    ),
                    locator=claim_locator(item),
                )
            )

    def _record_final_citations(self, event: AgentEvent) -> None:
        citations = event.payload.get("citations")
        if not isinstance(citations, dict):
            return
        for key, citation in citations.items():
            if not isinstance(citation, dict):
                continue
            claim_ref = str(citation.get("claim_ref") or "")
            if not claim_ref:
                continue
            marker = str(citation.get("marker") or f"[{key}]")
            self.final_citations.append(
                FinalCitationFact(
                    source_event_sequence=event.sequence,
                    marker=marker,
                    claim_ref=claim_ref,
                )
            )

    def _record_review(self, event: AgentEvent) -> None:
        result = event.payload.get("result")
        result = result if isinstance(result, dict) else {}
        score = result.get("overall_score")
        risks = result.get("risks")
        self.reviews.append(
            AnswerReviewFact(
                source_event_sequence=event.sequence,
                attempt=_as_int(event.payload.get("attempt"), default=1),
                max_attempts=_as_int(event.payload.get("max_attempts"), default=1),
                passed=bool(event.payload.get("passed")),
                retryable=bool(event.payload.get("retryable")),
                overall_score=score if isinstance(score, int) else None,
                risk_kinds=[
                    str(risk.get("kind"))
                    for risk in risks
                    if isinstance(risk, dict) and risk.get("kind")
                ]
                if isinstance(risks, list)
                else [],
            )
        )

    def _action_for_terminal_event(self, event: AgentEvent) -> ToolActionFact:
        tool_call_id = str(event.payload.get("id") or "")
        action = self._action(tool_call_id)
        if action is not None:
            return action
        tool_name = str(event.payload.get("name") or "")
        action = ToolActionFact(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=ToolActionStatus.RUNNING,
            arguments_sha256=_digest(event.payload.get("arguments")),
            retry_index=1 + sum(item.tool_name == tool_name for item in self.tool_actions),
        )
        self.tool_actions.append(action)
        return action

    def _action(self, tool_call_id: str) -> ToolActionFact | None:
        return next(
            (action for action in self.tool_actions if action.tool_call_id == tool_call_id),
            None,
        )


def build_run_evidence_ledger(events: Iterable[AgentEvent]) -> RunEvidenceLedger:
    ledger = RunEvidenceLedger()
    for event in events:
        ledger.apply(event)
    return ledger


def _digest(value: Any) -> str:
    return hashlib.sha256(_serialized_text(value).encode("utf-8")).hexdigest()


def _serialized_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _serialized_chars(value: Any) -> int:
    return len(_serialized_text(value))


def _candidate_locators(result: Any) -> list[EvidenceLocatorFact]:
    payload = _json_object(result)
    if payload is None:
        return []
    from agents.runtime.core.tools.base import unwrap_tool_data

    payload = unwrap_tool_data(payload)
    library_id = str(
        payload.get("library_id") or payload.get("workspace") or payload.get("workspace_id") or ""
    )
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = (
            payload.get("segments") or payload.get("video_segments") or payload.get("results")
        )
    if not isinstance(evidence, list):
        return []
    adjacent_segments = payload.get("adjacent_segments")
    if isinstance(adjacent_segments, list):
        evidence = [*evidence, *adjacent_segments]
    default_asset_id = str(payload.get("asset_id") or payload.get("document_id") or "")
    locators: list[EvidenceLocatorFact] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        asset_id = _mapping_asset_id(item) or default_asset_id
        segment_id = str(item.get("id") or item.get("segment_id") or item.get("chunk_id") or "")
        parts = item.get("parts")
        if not isinstance(parts, list):
            parts = [item]
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_metadata = part.get("metadata")
            part_metadata = part_metadata if isinstance(part_metadata, dict) else {}
            locator = EvidenceLocatorFact(
                library_id=library_id,
                asset_id=_mapping_asset_id(part) or asset_id,
                modality=str(part.get("kind") or item.get("modality") or ""),
                evidence_id=segment_id,
                segment_id=segment_id,
                time_range=_time_range(
                    part.get("time_range")
                    or part_metadata.get("time_range")
                    or item.get("time_range")
                    or metadata.get("time_range")
                ),
            )
            if _locator_has_identity(locator) and locator not in locators:
                locators.append(locator)
    return locators


def claim_locator(item: dict[str, Any]) -> EvidenceLocatorFact | None:
    """Resolve the source locator shared by current and trusted historical claims."""
    source = item.get("source")
    if not isinstance(source, dict):
        return None
    evidence_id = str(
        source.get("evidence_id") or source.get("chunk_id") or source.get("segment_id") or ""
    )
    locator = EvidenceLocatorFact(
        library_id=str(
            source.get("library_id") or source.get("workspace") or source.get("workspace_id") or ""
        ),
        asset_id=str(
            source.get("asset_id") or source.get("document_id") or source.get("doc_id") or ""
        ),
        modality=str(source.get("type") or item.get("tool") or ""),
        evidence_id=evidence_id,
        segment_id=str(source.get("segment_id") or evidence_id),
        time_range=_time_range(
            {
                "start_seconds": source.get("timestamp_start", item.get("timestamp_start")),
                "end_seconds": source.get("timestamp_end", item.get("timestamp_end")),
            }
        )
        or _time_range(source.get("time_range")),
    )
    return locator if _locator_has_identity(locator) else None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _time_range(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    start = value.get("start_seconds")
    end = value.get("end_seconds")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    return {"start_seconds": float(start), "end_seconds": float(end)}


def _mapping_asset_id(item: dict[str, Any]) -> str:
    """Asset id carried on a search hit, including HyperRAG's full_doc_id."""
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(
        item.get("asset_id")
        or item.get("document_id")
        or metadata.get("asset_id")
        or metadata.get("document_id")
        or metadata.get("full_doc_id")
        or ""
    ).strip()


def _locator_has_identity(locator: EvidenceLocatorFact) -> bool:
    return bool(
        locator.library_id
        or locator.asset_id
        or locator.evidence_id
        or locator.segment_id
        or locator.time_range
    )


def _bounded_text(value: Any, *, limit: int = 800) -> str:
    return _serialized_text(value)[:limit]


def _claim_key(item: dict[str, Any]) -> tuple[str, int | None, str]:
    citation_index = item.get("citation_index")
    return (
        str(item.get("claim_id") or ""),
        citation_index if isinstance(citation_index, int) else None,
        str(item.get("claim") or ""),
    )


def _claim_ref(run_id: str, tool_call_id: str, claim_id: str, citation_index: int | None) -> str:
    if claim_id:
        return f"claim:{run_id}:{tool_call_id}:{claim_id}"
    if citation_index is not None:
        return f"claim:{run_id}:{tool_call_id}:{citation_index}"
    return ""


def _incident_category(error: Any) -> ExecutionIncidentCategory:
    text = _serialized_text(error).lower()
    if any(token in text for token in ("upstream", "unavailable", "connection refused")):
        return ExecutionIncidentCategory.UPSTREAM_UNAVAILABLE
    if "timeout" in text:
        return ExecutionIncidentCategory.TIMEOUT
    if "permission" in text or "forbidden" in text:
        return ExecutionIncidentCategory.PERMISSION_DENIED
    if any(token in text for token in ("validation", "invalid argument", "valueerror")):
        return ExecutionIncidentCategory.VALIDATION_ERROR
    if any(token in text for token in ("parse", "jsondecode")):
        return ExecutionIncidentCategory.PARSE_ERROR
    return ExecutionIncidentCategory.INTERNAL_ERROR


_TOOL_ERROR_KIND_TO_CATEGORY = {
    "validation": ExecutionIncidentCategory.VALIDATION_ERROR,
    "timeout": ExecutionIncidentCategory.TIMEOUT,
    "upstream": ExecutionIncidentCategory.UPSTREAM_UNAVAILABLE,
    "permission": ExecutionIncidentCategory.PERMISSION_DENIED,
    "parse": ExecutionIncidentCategory.PARSE_ERROR,
    "internal": ExecutionIncidentCategory.INTERNAL_ERROR,
}


def _tool_result_error(result: Any) -> dict[str, Any] | None:
    """Read only the explicit common tool-error envelope, never legacy guesses."""

    payload = _json_object(result)
    if payload is None or payload.get("success") is not False:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    kind = error.get("kind")
    message = error.get("message")
    retryable = error.get("retryable")
    if (
        not isinstance(code, str)
        or not code
        or not isinstance(kind, str)
        or kind not in _TOOL_ERROR_KIND_TO_CATEGORY
        or not isinstance(message, str)
        or not message
        or not isinstance(retryable, bool)
    ):
        return None
    return {"code": code, "kind": kind, "retryable": retryable, "raw": error}


def _as_int(value: Any, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _claim_modalities(item: dict[str, Any], tool_name: str) -> list[str]:
    if tool_name == "evidence_asr":
        return ["asr", "text"]
    if tool_name == "evidence_ocr":
        return ["ocr", "text"]
    if tool_name in {"video_inspect", "video_inspect_2_0"}:
        return ["video"]
    if tool_name in {"document_inspect", "image_inspect", "visual_ground"}:
        return ["image"]
    source = item.get("source") or {}
    metadata = source.get("metadata") or {}
    kind = str(
        metadata.get("part_kind") or metadata.get("text_source") or source.get("type") or ""
    ).lower()
    if kind in {"asr", "subtitle", "subtitles", "transcript"}:
        return ["asr", "text"]
    # The current video index stores official subtitles in labelled ASR lines.
    # Require the verified quote itself to come from that field, not merely a
    # video chunk containing both visual captions and speech.
    if tool_name == "text_inspect":
        asr_lines = re.findall(r"(?im)^ASR:\s*(.+)$", str(metadata.get("chunk_content") or ""))
        quotes = [
            str(span.get("supporting_quote") or "")
            for span in metadata.get("evidence_spans", [])
            if isinstance(span, dict) and span.get("quote_verified")
        ]

        def normalized(text):
            return " ".join(text.casefold().split())

        if quotes and all(
            normalized(quote.removeprefix("ASR:").strip())
            and any(
                normalized(quote.removeprefix("ASR:").strip()) in normalized(line)
                for line in asr_lines
            )
            for quote in quotes
        ):
            return ["asr", "text"]
    if kind == "ocr":
        return ["ocr", "text"]
    return ["text"] if tool_name == "text_inspect" else []


def _compact_claim_source(source: dict[str, Any]) -> dict[str, Any]:
    """Retain provenance without duplicating raw retrieved chunk content in review."""
    keys = {
        "type",
        "library_id",
        "asset_id",
        "evidence_id",
        "segment_id",
        "uri",
        "timestamp_start",
        "timestamp_end",
        "time_range",
        "frame_ids",
        "frame_index",
        "fps",
        "crop",
        "bbox",
        "part_kind",
        "text_source",
    }
    compact = {key: value for key, value in source.items() if key in keys}
    metadata = source.get("metadata") or {}
    kept = {key: value for key, value in metadata.items() if key in keys}
    if kept:
        compact["metadata"] = kept
    return compact
