"""Core domain objects and runtime contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.answer_review import AnswerReviewConfig


class ToolCallRecord(BaseModel):
    """A runtime tool call paired with its returned tool message."""

    id: str
    name: str
    arguments: str
    result: str | None = None


class RuntimeBudgetConfig(BaseModel):
    """Per-profile per-run budget for recursion steps and wall-clock time.

    ``max_recursion_steps`` counts steps within a single run, not absolute
    LangGraph checkpoint steps that continue across a session thread.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_recursion_steps: int = Field(default=80, ge=1)
    source_coverage_max_recursion_steps: int | None = Field(default=None, ge=1)
    time_limit_seconds: float = Field(default=600, gt=0)
    warn_ratio: float = Field(default=0.8, gt=0, le=1)

    finalization_reserve_seconds: float = Field(default=0, ge=0)
    finalization_reserve_steps: int = Field(default=0, ge=0)
    capture_provisional_choice: bool = False
    choice_evidence_handoff: bool = False
    max_identical_attempts: int | None = Field(default=None, ge=1)
    # V4: cap on distinct broad-search scopes per run; None keeps V3 behaviour.
    max_broad_scopes: int | None = Field(default=None, ge=1)
    # V4: how many times a broad search may be redirected into verifying an
    # already advised candidate. None keeps V3 behaviour (never redirect).
    max_inspect_redirects: int | None = Field(default=None, ge=1)
    no_progress_warning_after: int = Field(default=0, ge=0)
    max_query_refine_calls: int | None = Field(default=None, ge=1)
    candidate_inspect_after: int = Field(default=0, ge=0)
    gap_driven_tools: bool = False

    @model_validator(mode="after")
    def validate_finalization_reserve(self):
        if self.finalization_reserve_seconds >= self.time_limit_seconds:
            raise ValueError("finalization reserve must be smaller than total run time")
        if self.finalization_reserve_steps >= self.max_recursion_steps:
            raise ValueError("finalization reserve must leave at least one exploration step")
        return self


class AgentProfile(BaseModel):
    """Runtime profile used to configure one kind of agent behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled_tools: tuple[str, ...] = ()
    enabled_skills: tuple[str, ...] = ()
    multi_agent_enabled: bool = False
    answer_review: AnswerReviewConfig = Field(default_factory=AnswerReviewConfig)
    runtime_budget: RuntimeBudgetConfig = Field(default_factory=RuntimeBudgetConfig)
    system_prompt: str | None = None
    user_prompt_template: str = "{input}"


class AgentOpType(StrEnum):
    """Runtime operation types."""

    USER_INPUT = "user_input"
    INTERRUPT = "interrupt"
    SHUTDOWN = "shutdown"


class TraceCaptureMode(StrEnum):
    """Controls optional eval trace capture during a run."""

    NONE = "none"
    FULL = "full"


class KnowledgeAssetSelection(BaseModel):
    """A user-selected knowledge asset supplied as run-scoped context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)


class AgentEventType(StrEnum):
    """Standard runtime event types."""

    RUN_STARTED = "run_started"
    USER_MESSAGE = "user_message"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    MODEL_CALL_STARTED = "model_call_started"
    REASONING_DELTA = "reasoning_delta"
    REASONING_COMPLETED = "reasoning_completed"
    CONTEXT_COMPACTED = "context_compacted"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_COMPLETED = "message_completed"
    STOP_GATE_EVALUATED = "stop_gate_evaluated"
    STOP_GATE_RELEASED = "stop_gate_released"
    TOOL_GATE_EVALUATED = "tool_gate_evaluated"
    ANSWER_REVIEW_STARTED = "answer_review_started"
    ANSWER_REVIEW_COMPLETED = "answer_review_completed"
    ANSWER_RETRY_STARTED = "answer_retry_started"
    RUN_BUDGET_WARNING = "run_budget_warning"
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"


class AgentStatusType(StrEnum):
    """Runtime and run status values."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SHUTDOWN = "shutdown"


class AgentOp(BaseModel):
    """Operation submitted to an agent runtime."""

    type: AgentOpType
    input: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    user_id: str = "local-user"
    workspace_id: str | None = "local-workspace"
    agent_model: str | None = None
    trace_capture: TraceCaptureMode = TraceCaptureMode.NONE
    langfuse_trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    langfuse_trace_source_id: str | None = Field(default=None, min_length=1)
    langfuse_trace_name: str | None = Field(default=None, min_length=1)
    memory_enabled: bool = False
    use_precompiled_knowledge: bool = True
    attachment_file_ids: list[str] | None = None
    library_ids: list[str] | None = None
    skill_flow_gold_trace: list[dict[str, Any]] | None = None
    selected_knowledge_assets: list[KnowledgeAssetSelection] = Field(default_factory=list)
    oracle_scope: dict[str, Any] | None = None
    oracle_task: dict[str, Any] | None = None


class SubmitResult(BaseModel):
    """Result returned after accepting a runtime operation."""

    run_id: str
    session_id: str | None = None


class AgentEvent(BaseModel):
    """Standard event emitted by an agent runtime."""

    type: AgentEventType
    run_id: str
    sequence: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentStatus(BaseModel):
    """Status maintained by an agent runtime."""

    status: AgentStatusType
    run_id: str | None = None
    session_id: str | None = None
    message: str | None = None
