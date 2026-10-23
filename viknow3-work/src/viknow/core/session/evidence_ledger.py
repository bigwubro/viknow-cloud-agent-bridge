"""Project session events into reusable historical verified claims."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from viknow.core.answer_review.claim_bundle import CLAIM_TOOL_NAMES, collect_claim_bundle
from viknow.core.runtime.domain import AgentEvent, AgentEventType, ToolCallRecord


class HistoricalClaim(BaseModel):
    """A verified claim from a previous session run."""

    claim_ref: str
    run_id: str
    tool_call_id: str
    tool: str
    claim: str
    source: dict[str, Any] = Field(default_factory=dict)
    render: dict[str, Any] = Field(default_factory=dict)
    display_label: str = ""
    claim_id: str = ""
    citation_index: int | None = None
    confidence: float | int | None = None
    quote_verified: bool | None = None
    verify_reason: str = ""
    created_at: str


class HistoricalMessageCitationMap(BaseModel):
    """Display citation mapping from a previous assistant answer."""

    text: str = ""
    citations: dict[str, dict[str, Any]] = Field(default_factory=dict)


def build_session_evidence_ledger(
    events: Iterable[AgentEvent],
    *,
    current_run_id: str | None = None,
    limit: int = 20,
) -> list[HistoricalClaim]:
    claims: list[HistoricalClaim] = []
    for event in events:
        if event.type != AgentEventType.TOOL_CALL_COMPLETED:
            continue
        if current_run_id and event.run_id == current_run_id:
            continue
        tool_name = str(event.payload.get("name") or "")
        if tool_name not in CLAIM_TOOL_NAMES:
            continue
        tool_call_id = str(event.payload.get("id") or "")
        if not tool_call_id:
            continue
        record = ToolCallRecord(
            id=tool_call_id,
            name=tool_name,
            arguments=str(event.payload.get("arguments") or ""),
            result=str(event.payload.get("result") or ""),
        )
        for item in collect_claim_bundle([record], filter_invalid=True):
            claim_ref = _claim_ref(event.run_id, tool_call_id, item)
            if not claim_ref:
                continue
            claims.append(
                HistoricalClaim(
                    claim_ref=claim_ref,
                    run_id=event.run_id,
                    tool_call_id=tool_call_id,
                    tool=str(item.get("tool") or tool_name),
                    claim=str(item.get("claim") or ""),
                    source=item.get("source") if isinstance(item.get("source"), dict) else {},
                    render=item.get("render") if isinstance(item.get("render"), dict) else {},
                    display_label=str(item.get("display_label") or ""),
                    claim_id=str(item.get("claim_id") or ""),
                    citation_index=_claim_citation_index(item),
                    confidence=item.get("confidence"),
                    quote_verified=item.get("quote_verified"),
                    verify_reason=str(item.get("verify_reason") or ""),
                    created_at=event.created_at.isoformat(),
                )
            )
    return list(reversed(claims))[:limit]


def build_session_message_citation_map(
    events: Iterable[AgentEvent],
    *,
    current_run_id: str | None = None,
) -> HistoricalMessageCitationMap:
    event_list = list(events)
    claim_refs_by_claim_id = _unique_claim_refs_by_claim_id(
        build_session_evidence_ledger(event_list, current_run_id=current_run_id, limit=1000)
    )
    for event in reversed(event_list):
        if event.type != AgentEventType.MESSAGE_COMPLETED:
            continue
        if current_run_id and event.run_id == current_run_id:
            continue
        citations = event.payload.get("citations")
        if not isinstance(citations, dict) or not citations:
            continue
        mapped = {}
        for marker, citation in citations.items():
            if not isinstance(citation, dict):
                continue
            item = dict(citation)
            claim_ref = str(item.get("claim_ref") or "")
            if not claim_ref:
                claim_id = str(item.get("claim_id") or "")
                claim_ref = claim_refs_by_claim_id.get(claim_id, "")
                if claim_ref:
                    item["claim_ref"] = claim_ref
            if item.get("claim_ref"):
                mapped[str(marker)] = item
        if mapped:
            return HistoricalMessageCitationMap(
                text=str(event.payload.get("text") or ""),
                citations=mapped,
            )
    return HistoricalMessageCitationMap()


def _unique_claim_refs_by_claim_id(claims: Iterable[HistoricalClaim]) -> dict[str, str]:
    refs: dict[str, str] = {}
    duplicated: set[str] = set()
    for claim in claims:
        if not claim.claim_id:
            continue
        if claim.claim_id in refs and refs[claim.claim_id] != claim.claim_ref:
            duplicated.add(claim.claim_id)
            continue
        refs[claim.claim_id] = claim.claim_ref
    for claim_id in duplicated:
        refs.pop(claim_id, None)
    return refs


def format_historical_claim_context(claims: Iterable[HistoricalClaim]) -> str:
    lines = ["历史已核验证据 claims："]
    for index, claim in enumerate(claims, start=1):
        lines.extend(
            [
                f"- ref: {claim.claim_ref}",
                f"  citation_token: [{historical_claim_alias(index)}]",
                f"  claim: {claim.claim}",
                f"  source: {claim.display_label or _source_label(claim.source)}",
                f"  tool: {claim.tool}",
                f"  scope: {_source_scope(claim.source)}",
                "  verified_at: previous turn",
            ]
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def historical_claim_alias(index: int) -> str:
    return f"H{index}"


def historical_claim_alias_map(claims: Iterable[HistoricalClaim]) -> dict[str, str]:
    return {
        claim.claim_ref: historical_claim_alias(index)
        for index, claim in enumerate(claims, start=1)
    }


def historical_claim_bundle(claims: Iterable[HistoricalClaim]) -> list[dict[str, Any]]:
    return [
        {
            "claim_ref": claim.claim_ref,
            "tool": claim.tool,
            "claim_id": claim.claim_id,
            "citation_index": claim.citation_index,
            "agent_citation": f"[{historical_claim_alias(index)}]",
            "display_label": claim.display_label,
            "source": claim.source,
            "render": claim.render,
            "segment_id": claim.display_label or _source_label(claim.source),
            "claim": claim.claim,
            "confidence": claim.confidence,
            "quote_verified": claim.quote_verified,
            "verify_reason": claim.verify_reason,
        }
        for index, claim in enumerate(claims, start=1)
    ]


def _claim_ref(run_id: str, tool_call_id: str, item: dict[str, Any]) -> str:
    claim_id = str(item.get("claim_id") or "")
    if claim_id:
        return f"claim:{run_id}:{tool_call_id}:{claim_id}"
    citation_index = _claim_citation_index(item)
    if citation_index is not None:
        return f"claim:{run_id}:{tool_call_id}:{citation_index}"
    return ""


def _claim_citation_index(item: dict[str, Any]) -> int | None:
    citation_index = item.get("citation_index")
    if isinstance(citation_index, int):
        return citation_index
    marker = str(item.get("agent_citation") or "").strip()
    if marker.startswith("[") and marker.endswith("]"):
        token = marker[1:-1]
        return int(token) if token.isdigit() else None
    return None


def _source_label(source: dict[str, Any]) -> str:
    for key in ("segment_id", "source_image_id", "evidence_id", "asset_id"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _source_scope(source: dict[str, Any]) -> str:
    source_type = str(source.get("type") or "evidence")
    start = source.get("timestamp_start")
    end = source.get("timestamp_end")
    if start is not None and end is not None:
        return f"{source_type} {start}-{end}s"
    return source_type
