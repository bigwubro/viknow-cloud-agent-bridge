"""Bounded database query and display contracts."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_http_base_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("expected HTTP(S) base URL without credentials, query or fragment")
    return value.rstrip("/")


class LangfuseSourceConfig(BaseModel):
    """Browser destination for one immutable Langfuse source identity."""

    model_config = ConfigDict(extra="forbid")

    ui_url: str | None = None
    ui_url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    project_id: str = Field(min_length=1, max_length=128)

    @field_validator("ui_url")
    @classmethod
    def validate_ui_url(cls, value: str | None) -> str | None:
        return _validate_http_base_url(value) if value is not None else None

    @model_validator(mode="after")
    def validate_url_source(self) -> LangfuseSourceConfig:
        if (self.ui_url is None) == (self.ui_url_env is None):
            raise ValueError("exactly one of ui_url or ui_url_env is required")
        return self

    def resolve_ui_url(self) -> str | None:
        value = self.ui_url if self.ui_url is not None else os.getenv(self.ui_url_env or "")
        return _validate_http_base_url(value) if value else None


class TraceConsoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool_size: int = Field(default=2, ge=1, le=8)
    query_timeout_seconds: float = Field(default=3, ge=0.1, le=10)
    refresh_seconds: int = Field(default=5, ge=5, le=60)
    langfuse_sources: dict[str, LangfuseSourceConfig] = Field(default_factory=dict)
    jaeger_ui_url: str | None = None
    jaeger_query_url: str | None = None
    jaeger_services: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("jaeger_ui_url", "jaeger_query_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_http_base_url(value)

    @field_validator("langfuse_sources")
    @classmethod
    def validate_source_ids(
        cls, value: dict[str, LangfuseSourceConfig]
    ) -> dict[str, LangfuseSourceConfig]:
        if any(not source_id.strip() or len(source_id) > 128 for source_id in value):
            raise ValueError("Langfuse source ids must contain 1 to 128 characters")
        return value


def decode_cursor(value: str) -> tuple[str, str]:
    try:
        created, run_id = json.loads(base64.urlsafe_b64decode(value.encode()))
        stamp = datetime.fromisoformat(created)
        if not stamp.tzinfo or not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            raise ValueError
        return stamp.astimezone(UTC).isoformat(), run_id
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid trace cursor") from exc


def encode_cursor(created: datetime, run_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created.isoformat(), run_id]).encode()).decode()


class TraceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=30, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)
    since: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC) - timedelta(days=1))
    until: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["running", "completed", "failed", "interrupted"] | None = None
    profile: str | None = Field(default=None, max_length=128)
    query: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=128)

    @field_validator("cursor")
    @classmethod
    def valid_cursor(cls, value):
        if value:
            decode_cursor(value)
        return value

    @model_validator(mode="after")
    def dates(self):
        if self.since >= self.until or self.until - self.since > timedelta(days=31):
            raise ValueError("select a time window of at most 31 days")
        return self


class TraceRuleAssessment(BaseModel):
    """Read-only candidate-rule projection shown before human review."""

    bucket: Literal["good", "bad", "uncertain"]
    risk: Literal["low", "medium", "high"]
    reason_codes: list[str] = Field(default_factory=list)


class TraceRow(BaseModel):
    id: str
    session_id: str
    profile_id: str | None
    status: str
    question_preview: str
    created_at: datetime
    started_at: datetime
    ended_at: datetime | None
    rule_assessment: TraceRuleAssessment | None = None


class TracePage(BaseModel):
    items: list[TraceRow]
    next_cursor: str | None = None


class TraceInterpretationUncertainty(BaseModel):
    issue: str
    possible_answers: list[str] = Field(default_factory=list)


class TraceSemanticAlignment(BaseModel):
    state: Literal["not_reviewed", "clear", "unresolved"] = "not_reviewed"
    original_question_check: str | None = None
    uncertainties: list[TraceInterpretationUncertainty] = Field(default_factory=list)
    review_is_current: bool = False
    authority: str | None = None


class TraceDetail(BaseModel):
    run: TraceRow
    question: str
    answer: str | None = None
    error: str | None = None
    run_kind: str = "unknown"
    behavior_snapshot: dict = Field(default_factory=dict)
    answer_review: dict = Field(default_factory=dict)
    answer_review_config: dict = Field(default_factory=dict)
    stop_gate: dict = Field(default_factory=dict)
    citations: dict = Field(default_factory=dict)
    token_usage: dict = Field(default_factory=dict)
    semantic_alignment: TraceSemanticAlignment = Field(default_factory=TraceSemanticAlignment)
    langfuse_traces: list[LangfuseTraceReference] = Field(default_factory=list)
    langfuse_reason: str | None = None


class LangfuseTraceReference(BaseModel):
    """Non-secret source identity persisted with a Langfuse trace id."""

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    service_id: str | None = None
    environment: str | None = None
    source_id: str | None = None
    project_id: str | None = None


class EventSummary(BaseModel):
    sequence: int
    type: str
    created_at: datetime


class EventPage(BaseModel):
    items: list[EventSummary]
    next_sequence: int | None = None


class ExternalLink(BaseModel):
    trace_id: str
    url: str


class TraceLinks(BaseModel):
    state: Literal["resolved", "not_configured", "not_found", "unavailable", "multiple"]
    links: list[ExternalLink] = Field(default_factory=list)
    message: str | None = None
