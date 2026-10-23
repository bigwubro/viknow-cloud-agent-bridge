"""Session history timeline projection for web UI."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.runtime.domain import AgentEvent, AgentEventType
from viknow.core.session.domain import RunNotFound, SessionNotFound, ToolCallNotFound
from viknow.core.session.event_utils import payload_text, run_tool_calls
from viknow.core.session.store import SessionStore

_SKIP_TIMELINE_EVENT_TYPES = frozenset(
    {
        AgentEventType.RUN_STARTED,
        AgentEventType.MODEL_CALL_STARTED,
        AgentEventType.RUN_BUDGET_WARNING,
        AgentEventType.RUN_BUDGET_EXHAUSTED,
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_INTERRUPTED,
        AgentEventType.MESSAGE_DELTA,
        AgentEventType.REASONING_DELTA,
    }
)

_REASONING_PREVIEW_CHARS = 120


class TokenUsageTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class TimelineItemBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    run_id: str
    sequence: int


class TimelineAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    file_name: str
    size_bytes: int = 0


class UserMessageTimelineItem(TimelineItemBase):
    kind: Literal["user_message"] = "user_message"
    text: str
    attachments: list[TimelineAttachment] = Field(default_factory=list)


class AssistantMessageTimelineItem(TimelineItemBase):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str
    token_usage: TokenUsageTimeline | None = None
    citations: dict[str, dict[str, object]] = Field(default_factory=dict)


class ToolCallTimelineItem(TimelineItemBase):
    kind: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    status: str
    duration_ms: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    agent: str | None = None
    agent_name: str | None = None


class ReasoningTimelineItem(TimelineItemBase):
    kind: Literal["reasoning"] = "reasoning"
    preview: str = ""
    has_detail: bool = True


class ContextCompactedTimelineItem(TimelineItemBase):
    kind: Literal["context_compacted"] = "context_compacted"
    file_path: str | None = None
    cutoff_index: int | None = None


class AnswerReviewTimelineItem(TimelineItemBase):
    kind: Literal["answer_review"] = "answer_review"
    phase: Literal["started", "completed"]
    attempt: int | None = None
    max_attempts: int | None = None
    passed: bool | None = None
    overall_score: float | None = None
    summary: str | None = None
    has_detail: bool = False


class AnswerRetryTimelineItem(TimelineItemBase):
    kind: Literal["answer_retry"] = "answer_retry"
    retry_attempt: int | None = None
    reason: str | None = None


class RunErrorTimelineItem(TimelineItemBase):
    kind: Literal["run_error"] = "run_error"
    message: str


TimelineItem = Annotated[
    UserMessageTimelineItem
    | AssistantMessageTimelineItem
    | ToolCallTimelineItem
    | ReasoningTimelineItem
    | ContextCompactedTimelineItem
    | AnswerReviewTimelineItem
    | AnswerRetryTimelineItem
    | RunErrorTimelineItem,
    Field(discriminator="kind"),
]


class SessionTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    items: list[TimelineItem]
    active_run_id: str | None = None
    active_run_after_sequence: int = 0


class ToolCallDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    name: str
    status: str
    arguments: str = ""
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    agent: str | None = None
    agent_name: str | None = None


class ReasoningDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    text: str
    format: str | None = None


class AnswerReviewRiskDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    message: str | None = None


class AnswerReviewClaimDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    text: str | None = None
    support_status: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_details: list[dict[str, object]] = Field(default_factory=list)
    notes: str | None = None


class AnswerReviewResultDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool | None = None
    overall_score: float | None = None
    dimension_scores: dict[str, float | None] | None = None
    summary: str | None = None
    citation_policy: dict[str, object] | None = None
    claims: list[AnswerReviewClaimDetail] = Field(default_factory=list)
    risks: list[AnswerReviewRiskDetail] = Field(default_factory=list)


class AnswerReviewDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    result: AnswerReviewResultDetail


def build_session_timeline(
    store: SessionStore,
    *,
    session_id: str,
) -> SessionTimeline:
    session = store.get_session(session_id)
    if session is None:
        raise SessionNotFound(session_id)

    items: list[TimelineItem] = []
    tool_items: dict[str, ToolCallTimelineItem] = {}
    run_message_completed: set[str] = set()
    run_max_sequence: dict[str, int] = {}
    events_by_run = _events_by_run(store, session_id)

    for run in store.list_runs_for_session(session_id):
        events = events_by_run.get(run.id, [])
        for event in events:
            run_max_sequence[event.run_id] = max(
                run_max_sequence.get(event.run_id, 0),
                event.sequence,
            )
            if event.type in _SKIP_TIMELINE_EVENT_TYPES:
                continue

            if event.type == AgentEventType.USER_MESSAGE:
                items.append(
                    UserMessageTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        text=payload_text(event.payload, "text"),
                        attachments=_attachments(event.payload),
                    )
                )
            elif event.type == AgentEventType.TOOL_CALL_STARTED:
                call_id = payload_text(event.payload, "id") or f"sequence-{event.sequence}"
                item_key = _tool_item_key(event.run_id, call_id)
                if item_key not in tool_items:
                    item = ToolCallTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        id=call_id,
                        name=payload_text(event.payload, "name") or "unknown_tool",
                        status="running",
                        started_at=event.created_at,
                    )
                    _apply_tool_agent(item, event.payload)
                    tool_items[item_key] = item
                    items.append(item)
                else:
                    item = tool_items[item_key]
                    if not item.name:
                        item.name = payload_text(event.payload, "name") or item.name
                    if item.started_at is None:
                        item.started_at = event.created_at
                    _apply_tool_agent(item, event.payload)
            elif event.type in {
                AgentEventType.TOOL_CALL_COMPLETED,
                AgentEventType.TOOL_CALL_FAILED,
            }:
                call_id = payload_text(event.payload, "id") or f"sequence-{event.sequence}"
                item = _find_tool_item(tool_items, event.run_id, event.payload, call_id)
                if item is None:
                    item = ToolCallTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        id=call_id,
                        name=payload_text(event.payload, "name") or "unknown_tool",
                        status="running",
                    )
                    _apply_tool_agent(item, event.payload)
                    tool_items[_tool_item_key(event.run_id, call_id)] = item
                    items.append(item)
                else:
                    item.id = call_id
                    tool_items[_tool_item_key(event.run_id, call_id)] = item
                name = payload_text(event.payload, "name")
                if name:
                    item.name = name
                _apply_tool_agent(item, event.payload)
                item.completed_at = event.created_at
                if item.started_at is not None:
                    delta = event.created_at - item.started_at
                    item.duration_ms = max(0, int(delta.total_seconds() * 1000))
                if event.type == AgentEventType.TOOL_CALL_COMPLETED:
                    item.status = "completed"
                else:
                    item.status = "failed"
            elif event.type == AgentEventType.REASONING_COMPLETED:
                text = payload_text(event.payload, "text")
                items.append(
                    ReasoningTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        preview=_preview(text),
                        has_detail=bool(text.strip()),
                    )
                )
            elif event.type == AgentEventType.CONTEXT_COMPACTED:
                cutoff = event.payload.get("cutoff_index")
                items.append(
                    ContextCompactedTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        file_path=payload_text(event.payload, "file_path") or None,
                        cutoff_index=cutoff if isinstance(cutoff, int) else None,
                    )
                )
            elif event.type == AgentEventType.MESSAGE_COMPLETED:
                run_message_completed.add(event.run_id)
                token_usage = _token_usage(event.payload.get("token_usage"))
                citations = event.payload.get("citations")
                items.append(
                    AssistantMessageTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        text=payload_text(event.payload, "text"),
                        token_usage=token_usage,
                        citations=citations if isinstance(citations, dict) else {},
                    )
                )
            elif event.type == AgentEventType.ANSWER_REVIEW_STARTED:
                items.append(
                    AnswerReviewTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        phase="started",
                        attempt=_int_or_none(event.payload.get("attempt")),
                        max_attempts=_int_or_none(event.payload.get("max_attempts")),
                    )
                )
            elif event.type == AgentEventType.ANSWER_REVIEW_COMPLETED:
                result = event.payload.get("result")
                if not isinstance(result, dict):
                    result = event.payload
                items.append(
                    AnswerReviewTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        phase="completed",
                        passed=_bool_or_none(result.get("passed")),
                        overall_score=_float_or_none(result.get("overall_score")),
                        summary=_optional_str(result.get("summary")),
                        has_detail=True,
                    )
                )
            elif event.type == AgentEventType.ANSWER_RETRY_STARTED:
                items.append(
                    AnswerRetryTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        retry_attempt=_int_or_none(event.payload.get("retry_attempt")),
                        reason=_optional_str(event.payload.get("reason")),
                    )
                )
            elif (
                event.type == AgentEventType.RUN_FAILED
                and event.run_id not in run_message_completed
            ):
                items.append(
                    RunErrorTimelineItem(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        message=payload_text(event.payload, "message") or "运行失败",
                    )
                )

    active_run_id = session.active_run_id
    active_run_after_sequence = (
        run_max_sequence.get(active_run_id, 0) if active_run_id is not None else 0
    )
    return SessionTimeline(
        session_id=session_id,
        items=items,
        active_run_id=active_run_id,
        active_run_after_sequence=active_run_after_sequence,
    )


def _apply_tool_agent(item: ToolCallTimelineItem, payload: dict[str, object]) -> None:
    agent = payload_text(payload, "agent")
    if agent:
        item.agent = agent
    agent_name = payload_text(payload, "agent_name")
    if agent_name:
        item.agent_name = agent_name


def _tool_item_key(run_id: str, call_id: str) -> str:
    return f"{run_id}:{call_id}"


def _find_tool_item(
    tool_items: dict[str, ToolCallTimelineItem],
    run_id: str,
    payload: dict[str, object],
    call_id: str,
) -> ToolCallTimelineItem | None:
    stream_id = payload_text(payload, "stream_id")
    for candidate in (stream_id, call_id):
        if not candidate:
            continue
        item = tool_items.get(_tool_item_key(run_id, candidate))
        if item is not None:
            return item
    return None


def get_tool_call_detail(
    store: SessionStore,
    *,
    session_id: str,
    call_id: str,
) -> ToolCallDetail:
    events_by_run = _events_by_run(store, session_id)
    for run in store.list_runs_for_session(session_id):
        events = events_by_run.get(run.id, [])
        for tool_call in run_tool_calls(events):
            if tool_call.id == call_id:
                started_at, completed_at = _tool_call_times(events, call_id)
                return ToolCallDetail(
                    id=tool_call.id,
                    run_id=run.id,
                    name=tool_call.name,
                    status=tool_call.status,
                    arguments=tool_call.arguments,
                    result=tool_call.result,
                    error=tool_call.error,
                    started_at=started_at,
                    completed_at=completed_at,
                    agent=tool_call.agent,
                    agent_name=tool_call.agent_name,
                )
    raise ToolCallNotFound(call_id)


def get_reasoning_detail(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str,
) -> ReasoningDetail:
    _ensure_run_in_session(store, session_id, run_id)
    events = _events_by_run(store, session_id).get(run_id, [])
    for event in events:
        if event.type == AgentEventType.REASONING_COMPLETED:
            return ReasoningDetail(
                run_id=run_id,
                text=payload_text(event.payload, "text"),
                format=_optional_str(event.payload.get("format")),
            )
    return ReasoningDetail(run_id=run_id, text="")


def get_answer_review_detail(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str,
) -> AnswerReviewDetail:
    _ensure_run_in_session(store, session_id, run_id)
    events = _events_by_run(store, session_id).get(run_id, [])
    for event in reversed(events):
        if event.type != AgentEventType.ANSWER_REVIEW_COMPLETED:
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            result = event.payload
        return AnswerReviewDetail(
            run_id=run_id,
            result=_answer_review_result(result),
        )
    return AnswerReviewDetail(run_id=run_id, result=AnswerReviewResultDetail())


def _events_by_run(
    store: SessionStore,
    session_id: str,
) -> dict[str, list[AgentEvent]]:
    events_by_run: dict[str, list[AgentEvent]] = defaultdict(list)
    for event in store.events_for_session(session_id):
        events_by_run[event.run_id].append(event)
    return events_by_run


def _ensure_run_in_session(store: SessionStore, session_id: str, run_id: str) -> None:
    if store.get_session(session_id) is None:
        raise SessionNotFound(session_id)
    if store.get_run(run_id) is None:
        raise RunNotFound(run_id)
    if store.get_run(run_id).session_id != session_id:
        raise RunNotFound(run_id)


def _tool_call_times(
    events: list[AgentEvent],
    call_id: str,
) -> tuple[datetime | None, datetime | None]:
    started_at: datetime | None = None
    completed_at: datetime | None = None
    call_ids = _tool_call_aliases(events, call_id)
    for event in events:
        event_call_id = payload_text(event.payload, "id")
        event_stream_id = payload_text(event.payload, "stream_id")
        if not call_ids.intersection({event_call_id, event_stream_id}):
            continue
        if event.type == AgentEventType.TOOL_CALL_STARTED:
            started_at = event.created_at
        elif event.type in {
            AgentEventType.TOOL_CALL_COMPLETED,
            AgentEventType.TOOL_CALL_FAILED,
        }:
            completed_at = event.created_at
    return started_at, completed_at


def _tool_call_aliases(events: list[AgentEvent], call_id: str) -> set[str]:
    aliases = {call_id}
    changed = True
    while changed:
        changed = False
        for event in events:
            event_call_id = payload_text(event.payload, "id")
            event_stream_id = payload_text(event.payload, "stream_id")
            if event_call_id in aliases and event_stream_id and event_stream_id not in aliases:
                aliases.add(event_stream_id)
                changed = True
            if event_stream_id in aliases and event_call_id and event_call_id not in aliases:
                aliases.add(event_call_id)
                changed = True
    return aliases


def _attachments(payload: dict[str, object]) -> list[TimelineAttachment]:
    raw = payload.get("attachments")
    if not isinstance(raw, list):
        return []
    items: list[TimelineAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_name = item.get("file_name")
        if not isinstance(file_name, str) or not file_name:
            continue
        file_id = item.get("file_id")
        if not isinstance(file_id, str):
            file_id = ""
        size_bytes = item.get("size_bytes")
        items.append(
            TimelineAttachment(
                file_id=file_id,
                file_name=file_name,
                size_bytes=size_bytes
                if isinstance(size_bytes, int) and not isinstance(size_bytes, bool)
                else 0,
            )
        )
    return items


def _preview(text: str) -> str:
    compact = text.strip()
    if len(compact) <= _REASONING_PREVIEW_CHARS:
        return compact
    return f"{compact[:_REASONING_PREVIEW_CHARS]}…"


def _token_usage(value: object) -> TokenUsageTimeline | None:
    if not isinstance(value, dict):
        return None
    return TokenUsageTimeline(
        input_tokens=_int_or_none(value.get("input_tokens")),
        output_tokens=_int_or_none(value.get("output_tokens")),
        total_tokens=_int_or_none(value.get("total_tokens")),
    )


def _answer_review_result(result: dict[str, object]) -> AnswerReviewResultDetail:
    dimension_scores = result.get("dimension_scores")
    parsed_scores: dict[str, float | None] | None = None
    if isinstance(dimension_scores, dict):
        parsed_scores = {str(key): _float_or_none(value) for key, value in dimension_scores.items()}
    risks: list[AnswerReviewRiskDetail] = []
    raw_risks = result.get("risks")
    if isinstance(raw_risks, list):
        for risk in raw_risks:
            if not isinstance(risk, dict):
                continue
            risks.append(
                AnswerReviewRiskDetail(
                    kind=_optional_str(risk.get("kind")),
                    message=_optional_str(risk.get("message")),
                )
            )
    claims: list[AnswerReviewClaimDetail] = []
    raw_claims = result.get("claims")
    if isinstance(raw_claims, list):
        for claim in raw_claims:
            if not isinstance(claim, dict):
                continue
            evidence_ids = claim.get("evidence_ids")
            parsed_evidence_ids = (
                [str(evidence_id) for evidence_id in evidence_ids]
                if isinstance(evidence_ids, list)
                else []
            )
            evidence_details = claim.get("evidence_details")
            parsed_evidence_details = (
                [
                    {str(key): value for key, value in detail.items()}
                    for detail in evidence_details
                    if isinstance(detail, dict)
                ]
                if isinstance(evidence_details, list)
                else []
            )
            claims.append(
                AnswerReviewClaimDetail(
                    id=_optional_str(claim.get("id")),
                    text=_optional_str(claim.get("text")),
                    support_status=_optional_str(claim.get("support_status")),
                    evidence_ids=parsed_evidence_ids,
                    evidence_details=parsed_evidence_details,
                    notes=_optional_str(claim.get("notes")),
                )
            )
    citation_policy = result.get("citation_policy")
    return AnswerReviewResultDetail(
        passed=_bool_or_none(result.get("passed")),
        overall_score=_float_or_none(result.get("overall_score")),
        dimension_scores=parsed_scores,
        summary=_optional_str(result.get("summary")),
        citation_policy=citation_policy if isinstance(citation_policy, dict) else None,
        claims=claims,
        risks=risks,
    )


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _bool_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
