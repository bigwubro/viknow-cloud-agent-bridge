"""DeepAgents kernel for ViKnow Agent Runtime."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import yaml
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.types import Command
from loguru import logger
from pydantic import PrivateAttr

from agents.runtime.agent_adapters.budget_efficiency import (
    BudgetEfficiencyMiddleware,
    choice_evidence_status,
    newer_stream_choice,
    observe_choice_stream,
)
from agents.runtime.agent_adapters.checkpointer import build_checkpointer_provider
from agents.runtime.agent_adapters.langfuse import (
    GenerationRecorder,
    register_trace_metadata_source,
    reset_active_generation_recorder,
    set_active_generation_recorder,
    unregister_trace_metadata_source,
)
from agents.runtime.agent_adapters.qa_agent_factory import create_qa_deep_agent
from agents.runtime.agent_adapters.qa_plan_middleware import (
    QaPlanMiddleware,
    QaPlanObservation,
    QaPlanSnapshot,
)
from agents.runtime.agent_adapters.tool_call_markup import (
    accumulate_stream_markup_buffer,
    is_tool_call_markup_end,
    is_tool_call_markup_fragment,
    is_tool_call_markup_start,
    maybe_recover_stream_tool_calls,
    recover_chat_result_tool_calls,
    reset_stream_markup_buffer,
    set_tool_call_recovery_context,
)
from agents.runtime.agent_adapters.tool_efficiency import ToolEfficiencyMiddleware, ToolEfficiencyState
from agents.runtime.core.answer_review import (
    AnswerReviewCitation,
    AnswerReviewCitationPolicy,
    AnswerReviewClaimRelevanceScorer,
    AnswerReviewClaimReviewer,
    AnswerReviewer,
    AnswerReviewReplayInput,
    AnswerReviewRequest,
    AnswerReviewResult,
    AnswerReviewSemanticReviewer,
    CitationPolicyClassifier,
    LLMClaimReviewer,
    LLMPolicyClassifier,
    LLMSemanticReviewer,
    RuleBasedAnswerReviewer,
    determine_citation_policy,
)
from agents.runtime.core.answer_review.choice import (
    ProvisionalChoice,
    has_final_choice,
    hold_final_choice,
    parse_choice_options,
    render_supported_choice,
    terminal_choice_answer,
    unverified_choice_answer,
)
from agents.runtime.core.answer_review.citation_resolver import resolve_citations
from agents.runtime.core.answer_review.claim_bundle import (
    CLAIM_TOOL_NAMES,
    VIDEO_INSPECT_TOOL_NAMES,
    citation_marker_index,
    citation_payload,
    collect_claim_bundle,
)
from agents.runtime.core.answer_review.domain import (
    STOP_GATE_CHECK_TOOL_NAME,
    AnswerReviewRisk,
    RiskSeverity,
)
from agents.runtime.core.answer_review.remediation import evidence_repair_risks, targeted_retry_prompt
from agents.runtime.core.answer_review.slot_coverage import resolve_slot_assessment
from agents.runtime.core.config import (
    AgentModelConfig,
    AgentModelOption,
    DeepAgentsKernelConfig,
    LangfuseRunBindingConfig,
    LangfuseUsagePushConfig,
    ModelProvider,
    ModelProviderConfig,
    ToolModelConfig,
    WorkspaceBackendType,
    resolve_model_config,
)
from agents.runtime.core.eval.research_oracle import (
    EventGroupingDecision,
    InspectionCallFact,
    InspectionFindingDecision,
    InspectionTask,
    ResearchPlan,
    ScoutReport,
    ScoutSegmentDecision,
    ScoutTask,
    build_event_map,
    build_inspection_report,
    build_scout_report,
)
from agents.runtime.core.eval.skill_flow import GoldToolSimulator, SkillEvalCase
from agents.runtime.core.evidence import Evidence
from agents.runtime.core.memory import UserMemoryService
from agents.runtime.core.memory.capability import build_memory_capability_notice
from agents.runtime.core.model_gateway import close_model_connections, model_connection_options
from agents.runtime.core.observability.otel import (
    ATTR_PHASE,
    ATTR_RUN_ID,
    ATTR_TOOL,
    ERROR_RUNTIME_BUDGET_EXHAUSTED,
    SpanSet,
    record_error,
    start_run_span,
    start_span,
)
from agents.runtime.core.runtime.answer_plan import AnswerPlanUpdate, RunAnswerPlan
from agents.runtime.core.runtime.answer_plan_tool import (
    AnswerPlanCommand,
    AnswerPlanObservation,
    AnswerPlanReview,
    RecordedAnswerPlanReview,
    answer_plan_error_result,
    answer_plan_goal_changes,
    answer_plan_view,
    evidence_digest,
)
from agents.runtime.core.runtime.behavior_snapshot import (
    RunBehaviorSnapshot,
    RunTraceLink,
    build_model_snapshot,
)
from agents.runtime.core.runtime.domain import (
    AgentEvent,
    AgentEventType,
    AgentOp,
    AgentOpType,
    AgentProfile,
    AgentStatus,
    AgentStatusType,
    KnowledgeAssetSelection,
    SubmitResult,
    ToolCallRecord,
    TraceCaptureMode,
)
from agents.runtime.core.runtime.events import (
    AgentEventMapper,
    AgentEventStore,
    AgentRuntime,
    InMemoryAgentEventStore,
)
from agents.runtime.core.runtime.evidence_ledger import (
    EvidenceClaimStatus,
    EvidenceLocatorFact,
    RunEvidenceLedger,
    _compact_claim_source,
    build_run_evidence_ledger,
    claim_locator,
)
from agents.runtime.core.runtime.qa_harness import answer_plan_judge_context
from agents.runtime.core.runtime.stop_gate import (
    LLMStopGateJudge,
    StopCandidateGate,
    StopGateDecision,
    StopGateInput,
    StopGateJudge,
    StopGateNextAction,
    StopGateReasonCode,
)
from agents.runtime.core.runtime.tool_gate import ToolGateContext
from agents.runtime.core.scheduler.config import CHAT_LLM_RESOURCE
from agents.runtime.core.scheduler.context import set_source_id
from agents.runtime.core.scheduler.domain import (
    Priority,
    SlotRequest,
    UnitKind,
    extra_body_with_priority,
    private_vllm_priority,
)
from agents.runtime.core.scheduler.resource_scheduler import ResourceScheduler
from agents.runtime.core.session.evidence_ledger import (
    HistoricalClaim,
    HistoricalMessageCitationMap,
    build_session_evidence_ledger,
    build_session_message_citation_map,
    historical_claim_alias,
    historical_claim_alias_map,
    historical_claim_bundle,
)
from agents.runtime.core.skills import SkillSpec
from agents.runtime.core.subagents import SubAgentSpec, exclusive_specialist_tool_owners
from agents.runtime.core.tools.base import (
    ToolBudgetUsage,
    ToolErrorCode,
    ToolResult,
    ToolRuntimeNotice,
    parse_tool_payload,
    unwrap_tool_data,
)
from agents.runtime.core.tools.context import ToolRunContext, reset_tool_run_context, set_tool_run_context
from agents.runtime.core.tools.workspace_artifacts import bind_workspace_artifacts


@dataclass
class _RunState:
    run_id: str
    session_id: str
    thread_id: str
    status: AgentStatusType
    queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    sequence: int = 0
    message: str | None = None
    task: asyncio.Task[None] | None = None
    streamed_text: str = ""
    streamed_reasoning: str = ""
    completed_message_emitted: bool = False
    completed_reasoning_emitted: bool = False
    context_compaction_keys: set[str] = field(default_factory=set)
    started_tool_call_ids: set[str] = field(default_factory=set)
    completed_tool_call_ids: set[str] = field(default_factory=set)
    failed_tool_call_ids: set[str] = field(default_factory=set)
    tool_call_id_aliases: dict[str, str] = field(default_factory=dict)
    completed_tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    historical_claims: list[HistoricalClaim] = field(default_factory=list)
    historical_claim_alias_refs: dict[str, str] = field(default_factory=dict)
    historical_message_citation_map: HistoricalMessageCitationMap = field(
        default_factory=HistoricalMessageCitationMap
    )
    model_token_usages: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_claim_citation_index: int = 1
    claim_citation_indexes: dict[str, int] = field(default_factory=dict)
    read_skills: list[str] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    inspect_results: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"text": [], "image": [], "video": [], "asr": [], "ocr": []}
    )
    budget_started_at: float = field(default_factory=time.monotonic)
    # Absolute LangGraph step at first observation in this run; used to derive
    # per-run relative recursion_step (checkpoint threads continue absolute steps).
    original_question: str = ""
    retry_instruction: str = ""
    best_effort_answer: str = ""
    choice_draft_start: int = 0
    choice_decision_seen: bool = False
    choice_source_message_id: str = ""
    choice_model_order: dict[str, int] = field(default_factory=dict)
    choice_message_runs: dict[str, str] = field(default_factory=dict)
    choice_stream_text: dict[str, str] = field(default_factory=dict)
    provisional_choice: ProvisionalChoice | None = None
    budget_finalizing: bool = False
    tool_efficiency: ToolEfficiencyState = field(default_factory=ToolEfficiencyState)
    original_op: AgentOp | None = None
    targeted_repair_started: bool = False
    repair_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    recursion_step_offset: int = 0
    recursion_step_base: int | None = None
    recursion_step: int = 0
    granted_recursion_step_limit: int = 0
    budget_warning_emitted: bool = False
    budget_exhausted: bool = False
    budget_abort: asyncio.Event = field(default_factory=asyncio.Event)
    budget_prompt_block: str = ""
    budget_warning_notice: ToolRuntimeNotice | None = None
    tool_internal_model_usage: list[dict[str, Any]] = field(default_factory=list)
    answer_review_trace_metadata: dict[str, Any] = field(default_factory=dict)
    usage_recorder: GenerationRecorder | None = None
    auto_visual_grounding_done: bool = False
    visual_ground_call_count: int = 0
    stop_gate: StopCandidateGate | None = None
    stop_gate_middleware_observed: bool = False
    stop_gate_check_count: int = 0
    # 每次 stop_gate_check 拦截的完整反馈，按工具调用 id 暂存，供工具结果
    # 填充与事件/trace 可观测性使用（args 只带最小触发参数，不再内嵌结果）。
    stop_gate_pending_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_gate_recoveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_final_candidate_text: str = ""
    last_answer_candidate: str = ""
    answer_candidates: list[str] = field(default_factory=list)
    published_final_answer: bool = False
    langfuse_trace_name: str | None = None
    evidence_ledger: RunEvidenceLedger = field(default_factory=RunEvidenceLedger)
    session_evidence_ledger: RunEvidenceLedger | None = None
    answer_plan: RunAnswerPlan | None = None
    answer_plan_review: RecordedAnswerPlanReview | None = None


@dataclass
class _FinalAnswerCitationContext:
    raw_answer: str
    resolved_answer: str
    publish_answer: str
    current_claims: list[dict[str, Any]]
    claim_bundle: list[dict[str, Any]]
    review_citations: list[AnswerReviewCitation]
    publish_citations: dict[str, dict[str, Any]]
    citation_errors: list[str]
    citation_policy: AnswerReviewCitationPolicy


@dataclass
class _WorkspaceRuntime:
    backend: Any
    skill_source: str | None = None
    subagent_skill_sources: dict[str, str] = field(default_factory=dict)
    close: Callable[[], None] | None = None


_SANDBOX_UID = 1000
_SANDBOX_GID = 1000
_WORKSPACE_ROOT = "/workspace"
_RUNTIME_SKILLS_DIR = ".skills"
_FILESYSTEM_SKILL_SOURCE = f"/{_RUNTIME_SKILLS_DIR}"
_BUBBLEWRAP_SKILL_SOURCE = f"{_WORKSPACE_ROOT}/{_RUNTIME_SKILLS_DIR}"
_MAX_RAW_MODEL_OUTPUT_CHARS = 50000
_MAX_RAW_MODEL_OUTPUT_DEPTH = 8
_DEFAULT_LANGFUSE_TRACE_NAME = "Viknow2"
_MODEL_USAGE_TRACE_FILE_ENV = "VIKNOW_MODEL_USAGE_TRACE_FILE"
_MODEL_USAGE_TRACE_INVOCATION: ContextVar[str | None] = ContextVar(
    "viknow_model_usage_trace_invocation",
    default=None,
)
_ANSWER_PLAN_OBSERVATION: ContextVar[AnswerPlanObservation | None] = ContextVar(
    "answer_plan_observation", default=None
)
_BUDGET_RECURSION_STEP_BUFFER = 6
_ORCHESTRATOR_SYSTEM_PROMPT = """
When specialist subagents are available, knowledge-base retrieval and
inspection are delegated. After reading the matching orchestrator skill,
call `task` once with `retriever`. If that report has keep_for_inspect
locators, call `task` once with `inspector`. Then write the user-facing
answer in a message with no tool calls. Do not call knowledge, search,
inspect, or visual_ground tools yourself. Do not use `general-purpose`.
A specialist report is a stage handoff, not a reason to launch another
task. `write_todos` is a notepad; completing todos does not finish the
turn and must not trigger another `task`.
""".strip()

_ORCHESTRATOR_TASK_DESCRIPTION = """Launch one specialist for the current
knowledge-QA stage, then wait for its report.

Available specialists:
{available_agents}

Use `subagent_type=retriever` once to collect locators. If the report has
keep_for_inspect locators, use `subagent_type=inspector` once. Then write
the user-facing answer yourself.

Do not use `general-purpose`. Do not run retriever and inspector in parallel.
Do not launch another specialist after a complete report arrives. A
`need_more_locator` report means write an evidence-insufficient answer, not
another retrieval task.

The specialist result is not visible to the user. After the pipeline, send
one text message that answers the user. That message must have no tool calls.
""".strip()

_ORCHESTRATOR_TASK_SYSTEM_PROMPT = """## `task`

`task` runs one specialist and returns a stage report to you. The user does
not see that report.

Closed loop: retriever once, then inspector once only if locators exist, then
you write the final answer. Never use `general-purpose`. Never launch another
`task` after a complete specialist report. Completing `write_todos` is not a
reason to call `task` again.
""".strip()

_SKILL_DIRECTED_ORCHESTRATOR_SYSTEM_PROMPT = """
When specialist subagents are available, read the matching project skill before
delegating. The skill determines the workflow, allowed specialist roles, and
terminal artifact. Do not call a specialist that the active skill does not
allow, and do not substitute a user-facing answer for a workflow artifact.
""".strip()

_SKILL_DIRECTED_TASK_DESCRIPTION = """Launch the specialist required by the
active project skill and current workflow state.

Available specialists:
{available_agents}

Follow the active skill's delegation order, scope boundaries, and terminal
artifact requirements. Do not use `general-purpose` unless the active skill
explicitly allows it. Specialist reports are internal handoffs, not
user-visible answers.
""".strip()

_SKILL_DIRECTED_TASK_SYSTEM_PROMPT = """## `task`

`task` runs the specialist selected by the active project skill and returns an
internal stage report. Respect the active skill's scope and handoff contract.
""".strip()

_DISABLED_GENERAL_PURPOSE_SUBAGENT = {
    "name": "general-purpose",
    "description": (
        "Do not use. Knowledge QA only uses retriever then inspector. "
        "Never set subagent_type to general-purpose."
    ),
    "system_prompt": (
        "This agent type is disabled for knowledge QA. Reply that the main "
        "agent must use retriever or inspector, then stop."
    ),
    "tools": [],
}


_MAIN_AGENT_NAMES = frozenset({"", "DeepAgent", "deep_agent", "LangGraph", "main"})


def _metadata_lookup(metadata: Any, *keys: str) -> Any:
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        if key in metadata:
            return metadata.get(key)
    configurable = metadata.get("configurable")
    if isinstance(configurable, dict):
        for key in keys:
            if key in configurable:
                return configurable.get(key)
    return None


def _subagent_name(metadata: Any) -> str:
    value = _metadata_lookup(metadata, "lc_agent_name", "ls_agent_name")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _task_subagent_type(arguments: Any) -> str:
    data = arguments
    if isinstance(data, str):
        raw = data.strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'"subagent_type"\s*:\s*"([^"]+)"', raw)
            return match.group(1).strip() if match else ""
    if not isinstance(data, dict):
        return ""
    for candidate in (data, data.get("args") if isinstance(data.get("args"), dict) else None):
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("subagent_type") or candidate.get("subagentType")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _agent_attribution(
    metadata: Any,
    known_names: Iterable[str] = (),
) -> dict[str, str]:
    if _is_subagent_metadata(metadata, known_names=known_names):
        payload = {"agent": "subagent"}
        name = _subagent_name(metadata)
        if name:
            payload["agent_name"] = name
        return payload
    return {"agent": "main"}


def _started_tool_payload(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    started: dict[str, Any] = {
        "id": call_id,
        "name": payload.get("name") or "",
        "arguments": payload.get("arguments") or "",
    }
    for key in ("agent", "agent_name"):
        value = payload.get(key)
        if value:
            started[key] = value
    return started


def _is_subagent_metadata(
    metadata: Any,
    known_names: Iterable[str] = (),
) -> bool:
    if not isinstance(metadata, dict):
        return False
    if _metadata_lookup(metadata, "ls_agent_type") == "subagent":
        return True
    name = _subagent_name(metadata)
    if not name or name in _MAIN_AGENT_NAMES:
        return False
    names = frozenset(item for item in known_names if item)
    if names:
        return name in names
    return True


def _is_subagent_event(
    event: dict[str, Any],
    known_names: Iterable[str] = (),
) -> bool:
    return _is_subagent_metadata(event.get("metadata"), known_names=known_names)


_TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_INTERRUPTED,
    }
)


def _event_langgraph_step(event: dict[str, Any]) -> int | None:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    step = metadata.get("langgraph_step")
    return step if isinstance(step, int) else None


def _run_original_question(state: _RunState) -> str:
    """Read task identity from the graph projection; never from a retry prompt."""
    return state.answer_plan.original_question if state.answer_plan else state.original_question


def _review_evidence_facts(state: _RunState) -> list[dict[str, Any]]:
    """Reuse session-verified claims without recording a new current-run inspection."""
    from agents.runtime.core.runtime.evidence_ledger import _claim_modalities, _compact_claim_source

    facts = [fact.model_dump(mode="json") for fact in state.evidence_ledger.claims]
    seen = {fact["claim_ref"] for fact in facts}
    for claim in state.historical_claims:
        if claim.claim_ref in seen:
            continue
        item = {"source": claim.source, "tool": claim.tool}
        locator = claim_locator(item)
        facts.append(
            {
                "claim_ref": claim.claim_ref,
                "claim": claim.claim,
                "status": "verified",
                "run_id": claim.run_id,
                "tool_call_id": claim.tool_call_id,
                "tool_name": claim.tool,
                "source": _compact_claim_source(claim.source),
                "modalities": _claim_modalities(item, claim.tool),
                "locator": locator.model_dump(mode="json") if locator else None,
                "citation": f"[{claim.claim_ref}]",
            }
        )
        seen.add(claim.claim_ref)
    return facts


def _record_review_execution_failure(state, status: str, attempt: int) -> None:
    """Preserve factual coverage when the reviewer cannot assess it."""
    for slot in _run_answer_slots(state):
        previous = state.evidence_ledger.slot_evidence.get(slot["id"], {})
        state.evidence_ledger.slot_evidence[slot["id"]] = {
            **previous,
            "slot_id": slot["id"],
            "review_execution_status": status,
        }
    state.repair_diagnostics.append(
        {
            "action": "publish_unreviewed",
            "review_execution_status": status,
            "attempt": attempt,
            "claim_refs": [claim.claim_ref for claim in state.evidence_ledger.claims],
        }
    )


def _run_answer_slots(state: _RunState) -> list[dict[str, Any]]:
    """Prefer the graph-owned plan; preserve slots for standalone video profiles."""
    if state.answer_plan:
        return [
            slot.model_dump(
                mode="json",
                include={
                    "id",
                    "subquestion",
                    "context",
                    "required_modalities",
                    "optional_modalities",
                    "coverage_condition",
                    "role",
                    "answer_unit",
                },
            )
            for slot in state.answer_plan.slots
        ]
    if state.evidence_ledger.answer_slots:
        # Standalone advanced-video profiles have no graph-owned answer_plan.
        # Preserve their existing query-refine slots; native QA uses its plan above.
        return [dict(slot) for slot in state.evidence_ledger.answer_slots]
    question = _run_original_question(state)
    return (
        [
            {
                "id": "original",
                "subquestion": question,
                "context": question,
                "required_modalities": [],
                "optional_modalities": [],
                "coverage_condition": "Answer every constraint in the original question.",
            }
        ]
        if question
        else []
    )


def _observe_run_recursion_step(state: _RunState, langgraph_step: int) -> bool:
    """Map absolute langgraph_step to per-run relative recursion_step.

    LangGraph checkpoint threads keep incrementing absolute steps across runs in
    the same session. Runtime budget limits must use the delta within this run.
    Returns True when the relative step advanced.
    """
    if state.recursion_step_base is None:
        state.recursion_step_base = langgraph_step
    relative = state.recursion_step_offset + langgraph_step - state.recursion_step_base + 1
    if relative <= state.recursion_step:
        return False
    state.recursion_step = relative
    return True


@dataclass
class _RunModelUsageSink:
    by_invocation: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, invocation_id: str | None, usage: dict[str, Any]) -> None:
        normalized = _normalize_token_usage(usage)
        if not normalized:
            return
        self.by_invocation[invocation_id or "__anonymous__"] = normalized

    def aggregate(self) -> dict[str, Any]:
        return _aggregate_token_usages(self.by_invocation.values())


_MODEL_USAGE_SINK: ContextVar[_RunModelUsageSink | None] = ContextVar(
    "viknow_model_usage_sink",
    default=None,
)
_RUN_BUDGET_ABORT: ContextVar[asyncio.Event | None] = ContextVar(
    "viknow_run_budget_abort",
    default=None,
)
_EXCLUDED_WORKSPACE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".skills",
    "__pycache__",
    "node_modules",
}
_RESTORE_WORKSPACE_PREFIXES = ("uploads",)
_CAPTURE_WORKSPACE_PREFIXES = ("outputs", "scratch")
_INSPECT_TOOL_NAMES = CLAIM_TOOL_NAMES
_SEARCH_TOOL_NAMES = {"knowledge_search"}
_COUNTING_QUESTION_RE = re.compile(
    r"(几片|几个|几条|几种|几次|几项|几位|几层|几台|几支|几张|几根|几颗|几瓣|几块|"
    r"多少(?:个|种|次|项|位|层|台|支|张|根|颗|瓣|块)?|数量|计数|"
    r"how\s+many|count|number\s+of)",
    re.IGNORECASE,
)
_NON_STATIC_COUNT_RE = re.compile(
    r"(几种|多少种|种类|几次|多少次|出现.{0,8}(?:几次|多少次)|"
    r"how\s+many\s+times|number\s+of\s+times|"
    r"几位|小数|多久|时长|持续|周期|多少(?:秒|分钟|小时)|"
    r"字幕|文字|台词|参数|功能|工具|圈|转动|擦拭|流程中|"
    r"使用了|用了|耗用了|共用|要检查|拔了几个|拆了几个)",
    re.IGNORECASE,
)
_HISTORICAL_ALIAS_MARKER_RE = re.compile(r"\[H(\d+)\]")
_STABLE_CLAIM_MARKER_RE = re.compile(r"\[claim:[^\]\s]+\]")
_ANSWER_CITATION_MARKER_RE = re.compile(r"\[(?:\d+|H\d+|claim:[^\]\s]+)\]")


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _log_answer_review_stage(
    state: _RunState,
    stage: str,
    *,
    level: str = "info",
    **extra: Any,
) -> None:
    bound = logger.bind(
        viknow_run_id=state.run_id,
        viknow_session_id=state.session_id,
        viknow_thread_id=state.thread_id,
        answer_review_stage=stage,
        **extra,
    )
    message = _answer_review_log_message(stage, extra)
    if level == "warning":
        bound.warning(message)
    else:
        bound.info(message)


def _answer_review_log_message(stage: str, extra: dict[str, Any]) -> str:
    parts = [f"[answer_review] {stage}"]
    for key in ("reason", "error", "passed", "overall_score", "elapsed_ms"):
        value = extra.get(key)
        if value is not None and value != "":
            parts.append(f"{key}={value}")
    return " ".join(parts)


class ViKnowChatOpenAI(ChatOpenAI):
    """OpenAI-compatible chat model adapter with ViKnow provider compatibility fixes."""

    _trace_file: Path | None = PrivateAttr(default=None)
    _reasoning_content_roundtrip: bool = PrivateAttr(default=False)

    def __init__(
        self,
        *,
        usage_trace_file: Path | None = None,
        reasoning_content_roundtrip: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._trace_file = usage_trace_file
        self._reasoning_content_roundtrip = reasoning_content_roundtrip

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self._reasoning_content_roundtrip and payload.get("tools"):
            source_messages = self._convert_input(input_).to_messages()
            request_messages = payload.get("messages")
            if isinstance(request_messages, list):
                for source, request in zip(source_messages, request_messages, strict=True):
                    if not isinstance(source, AIMessage) or not isinstance(request, dict):
                        continue
                    reasoning = source.additional_kwargs.get("reasoning_content")
                    request["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
        if payload.get("stream"):
            stream_options = dict(payload.get("stream_options") or {})
            stream_options["include_usage"] = True
            payload["stream_options"] = stream_options
        invocation_id = str(uuid4())
        _MODEL_USAGE_TRACE_INVOCATION.set(invocation_id)
        reset_stream_markup_buffer()
        set_tool_call_recovery_context(
            invocation_id=invocation_id,
            model=getattr(self, "model_name", None) or payload.get("model"),
        )
        if self._trace_file is not None:
            _write_model_usage_trace(
                self._trace_file,
                _model_usage_request_trace(invocation_id, payload),
            )
        from agents.runtime.core.eval.recorder import active_model_call_trace_recorder

        active_model_call_trace_recorder().on_model_request(payload)
        return payload

    def _create_chat_result(
        self,
        response: dict[str, Any] | Any,
        generation_info: dict[str, Any] | None = None,
    ) -> Any:
        result = super()._create_chat_result(response, generation_info)
        return recover_chat_result_tool_calls(result)

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> Any:
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            invocation_id = _MODEL_USAGE_TRACE_INVOCATION.get()
            from agents.runtime.core.eval.recorder import active_model_call_trace_recorder

            active_model_call_trace_recorder().on_model_usage(
                usage,
                invocation_id=invocation_id,
            )
            sink = _MODEL_USAGE_SINK.get()
            if sink is not None:
                sink.record(invocation_id, usage)
            if self._trace_file is not None:
                _write_model_usage_trace(
                    self._trace_file,
                    _model_usage_stream_chunk_trace(
                        invocation_id,
                        chunk,
                    ),
                )
        reasoning = _extract_openai_stream_chunk_reasoning(chunk)
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            _normalize_openai_stream_usage_chunk(chunk),
            default_chunk_class,
            base_generation_info,
        )
        if reasoning and generation_chunk is not None:
            _attach_reasoning_to_generation_chunk(generation_chunk, reasoning)
        if generation_chunk is not None:
            accumulate_stream_markup_buffer(generation_chunk, reasoning)
            maybe_recover_stream_tool_calls(generation_chunk)
        return generation_chunk


class ScheduledViKnowChatOpenAI(ViKnowChatOpenAI):
    """Main agent chat model that acquires scheduler slots for private backends."""

    _scheduler: ResourceScheduler | None = PrivateAttr(default=None)
    _resource_class: str | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        scheduler: ResourceScheduler | None = None,
        resource_class: str | None = None,
        usage_trace_file: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(usage_trace_file=usage_trace_file, **kwargs)
        self._scheduler = scheduler
        self._resource_class = resource_class

    def _slot_request(self) -> SlotRequest | None:
        if self._scheduler is None or not self._resource_class:
            return None
        from agents.runtime.core.scheduler.context import get_source_id

        return SlotRequest(
            source_id=get_source_id(),
            priority=Priority.INTERACTIVE,
            resources=(self._resource_class,),
            unit_kind=UnitKind.MODEL,
            metadata={"model_role": "main_agent", "model": self.model_name},
        )

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        request = self._slot_request()
        if request is None:
            return await super()._agenerate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
        async with self._scheduler.acquire(request):
            return await super()._agenerate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )

    async def _astream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        request = self._slot_request()
        abort = _RUN_BUDGET_ABORT.get()
        if request is None:
            async for chunk in _yield_until_abort(
                super()._astream(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                ),
                abort,
            ):
                yield chunk
            return
        async with self._scheduler.acquire(request):
            async for chunk in _yield_until_abort(
                super()._astream(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                ),
                abort,
            ):
                if abort is not None and abort.is_set():
                    break
                yield chunk


def _normalize_openai_stream_usage_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    if chunk.get("usage") is None or _is_final_openai_stream_chunk(chunk):
        return chunk
    normalized = dict(chunk)
    normalized.pop("usage", None)
    return normalized


def _is_final_openai_stream_chunk(chunk: dict[str, Any]) -> bool:
    choices = _openai_stream_chunk_choices(chunk)
    if not choices:
        return True
    if any(
        isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in choices
    ):
        return True
    # vllm 等实现在流末尾追加 usage chunk，其 choices 带 finish_reason=null 且 delta 无内容，
    # 与 OpenAI 标准的空 choices 等价；不识别则 usage 会被当作中间 chunk 剥掉。
    return chunk.get("usage") is not None and not _openai_stream_chunk_has_delta_content(chunk)


def _openai_stream_chunk_has_delta_content(chunk: dict[str, Any]) -> bool:
    for choice in _openai_stream_chunk_choices(chunk):
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if content not in (None, "", [], {}):
            return True
        if delta.get("tool_calls"):
            return True
    return False


def _openai_stream_chunk_choices(chunk: dict[str, Any]) -> list[Any]:
    choices = chunk.get("choices")
    if isinstance(choices, list):
        return choices
    nested_chunk = chunk.get("chunk")
    if isinstance(nested_chunk, dict):
        nested_choices = nested_chunk.get("choices")
        if isinstance(nested_choices, list):
            return nested_choices
    return []


def _extract_openai_stream_chunk_reasoning(chunk: dict[str, Any]) -> str:
    parts: list[str] = []
    for choice in _openai_stream_chunk_choices(chunk):
        if not isinstance(choice, dict):
            continue
        reasoning = _extract_reasoning(choice.get("delta"))
        if reasoning:
            parts.append(reasoning)
    return "".join(parts)


def _attach_reasoning_to_generation_chunk(generation_chunk: Any, reasoning: str) -> None:
    message = getattr(generation_chunk, "message", None)
    if message is None or _extract_reasoning(message):
        return
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        additional_kwargs = {}
        message.additional_kwargs = additional_kwargs
    additional_kwargs["reasoning_content"] = reasoning


def _write_model_usage_trace(trace_file: Path, record: dict[str, Any]) -> None:
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    with trace_file.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str))
        file.write("\n")


def _model_usage_request_trace(invocation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    message_records = (
        [_model_usage_message_trace(message) for message in messages]
        if isinstance(messages, list)
        else []
    )
    tools = payload.get("tools")
    return {
        "event": "request",
        "invocation_id": invocation_id,
        "model": payload.get("model"),
        "stream": payload.get("stream"),
        "stream_usage_requested": (
            (payload.get("stream_options") or {}).get("include_usage")
            if isinstance(payload.get("stream_options"), dict)
            else None
        ),
        "message_count": len(message_records),
        "message_content_chars": sum(item["content_chars"] for item in message_records),
        "message_tool_call_chars": sum(item["tool_call_chars"] for item in message_records),
        "messages": message_records,
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "tools_schema_chars": _json_chars(tools) if tools is not None else 0,
    }


def _model_usage_message_trace(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        return {
            "role": str(message.get("role") or ""),
            "name": str(message.get("name") or ""),
            "content_chars": _content_chars(content),
            "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            "tool_call_chars": _json_chars(tool_calls) if tool_calls is not None else 0,
        }
    content = getattr(message, "content", None)
    tool_calls = getattr(message, "tool_calls", None)
    return {
        "role": type(message).__name__,
        "name": str(getattr(message, "name", "") or ""),
        "content_chars": _content_chars(content),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "tool_call_chars": _json_chars(tool_calls) if tool_calls is not None else 0,
    }


def _model_usage_stream_chunk_trace(
    invocation_id: str | None,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        nested = chunk.get("chunk")
        choices = nested.get("choices") if isinstance(nested, dict) else []
    return {
        "event": "stream_chunk_usage",
        "invocation_id": invocation_id,
        "model": chunk.get("model"),
        "choice_count": len(choices) if isinstance(choices, list) else 0,
        "finish_reasons": [
            choice.get("finish_reason")
            for choice in choices
            if isinstance(choice, dict) and choice.get("finish_reason") is not None
        ],
        "usage": _raw_model_output_value(chunk.get("usage")),
    }


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    total += len(block["text"])
                else:
                    total += len(repr(block))
            else:
                total += len(repr(block))
        return total
    return len(repr(content))


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


class DeepAgentsEventMapper(AgentEventMapper):
    """Map DeepAgents/LangGraph events and results into ViKnow runtime events."""

    def __init__(
        self,
        *,
        subagent_names: Iterable[str] = (),
        tool_owners: dict[str, str] | None = None,
    ) -> None:
        self._suppressing_internal_tool_call_markup = False
        self._subagent_names = frozenset(name for name in subagent_names if name)
        self._tool_owners = dict(tool_owners or {})
        self._active_task_agents: dict[str, str] = {}

    def bind_subagent_names(self, names: Iterable[str]) -> None:
        self._subagent_names = frozenset(name for name in names if name)

    def bind_tool_owners(self, tool_owners: dict[str, str]) -> None:
        self._tool_owners = dict(tool_owners)

    def is_subagent_event(self, event: dict[str, Any]) -> bool:
        name = str(event.get("name") or "")
        if name in self._subagent_names or name in self._tool_owners:
            return True
        return (
            _is_subagent_event(event, known_names=self._subagent_names)
            or bool(self._agent_from_parent_ids(event))
            or bool(self._agent_from_checkpoint_ns(event.get("metadata")))
        )

    def _agent_from_parent_ids(self, event: dict[str, Any]) -> dict[str, str]:
        parent_ids = event.get("parent_ids")
        if not isinstance(parent_ids, (list, tuple)):
            parent_id = event.get("parent_run_id")
            parent_ids = [parent_id] if parent_id else []
        for parent_id in parent_ids:
            name = self._active_task_agents.get(str(parent_id or ""))
            if name:
                return {"agent": "subagent", "agent_name": name}
        return {}

    def _agent_from_checkpoint_ns(self, metadata: Any) -> dict[str, str]:
        raw = _metadata_lookup(metadata, "langgraph_checkpoint_ns", "checkpoint_ns")
        if not isinstance(raw, str) or not raw:
            return {}
        parts = {part for part in re.split(r"[|:]", raw) if part}
        for name in self._subagent_names:
            if name in parts:
                return {"agent": "subagent", "agent_name": name}
        return {}

    def _agent_from_tool_owner(self, name: str) -> dict[str, str]:
        owner = self._tool_owners.get(name)
        if not owner:
            return {}
        return {"agent": "subagent", "agent_name": owner}

    def _remember_task_agent(self, event: dict[str, Any], name: str, arguments: Any) -> str:
        delegated = _task_subagent_type(arguments)
        run_id = str(event.get("run_id") or "")
        if name == "task" and run_id and delegated:
            self._active_task_agents[run_id] = delegated
        return delegated

    def _forget_task_agent(self, event: dict[str, Any], name: str) -> None:
        if name != "task":
            return
        run_id = str(event.get("run_id") or "")
        if run_id:
            self._active_task_agents.pop(run_id, None)

    def _tool_attribution(
        self,
        event: dict[str, Any],
        name: str,
        arguments: Any,
        *,
        closing: bool = False,
    ) -> dict[str, str]:
        delegated = self._remember_task_agent(event, name, arguments)
        if name == "task":
            attribution = {"agent": "main"}
            if delegated:
                attribution["agent_name"] = delegated
            if closing:
                self._forget_task_agent(event, name)
            return attribution
        attribution = self._agent_from_tool_owner(name) or _agent_attribution(
            event.get("metadata"), self._subagent_names
        )
        if attribution.get("agent") != "subagent":
            attribution = (
                self._agent_from_parent_ids(event)
                or self._agent_from_checkpoint_ns(event.get("metadata"))
                or attribution
            )
        if closing:
            self._forget_task_agent(event, name)
        return attribution

    def map_stream_delta(self, delta: Any) -> tuple[AgentEventType, dict[str, Any]] | None:
        text = _extract_text(delta)
        if not text or self._should_suppress_internal_tool_call_markup(text):
            return None
        return AgentEventType.MESSAGE_DELTA, {"text": text}

    def map_stream_reasoning(self, delta: Any) -> tuple[AgentEventType, dict[str, Any]] | None:
        text = _extract_reasoning(delta)
        if not text or self._should_suppress_internal_tool_call_markup(text):
            return None
        return (
            AgentEventType.REASONING_DELTA,
            {"text": text, "format": "reasoning_content"},
        )

    def map_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        metadata: Any = None,
        attribution: dict[str, str] | None = None,
    ) -> tuple[AgentEventType, dict[str, Any]]:
        return (
            AgentEventType.TOOL_CALL_STARTED,
            {
                "id": call_id,
                "name": name,
                "arguments": _serialize(arguments),
                **(attribution or _agent_attribution(metadata, self._subagent_names)),
            },
        )

    def map_tool_call_completed(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        result: Any,
        metadata: Any = None,
        attribution: dict[str, str] | None = None,
    ) -> tuple[AgentEventType, dict[str, Any]]:
        payload = {
            "id": call_id,
            "name": name,
            "arguments": _serialize(arguments),
            "result": _serialize(result),
            **(attribution or _agent_attribution(metadata, self._subagent_names)),
        }
        debug_result = _tool_result_full_result(result)
        if debug_result is not None:
            payload["debug_result"] = debug_result
        artifact = _tool_result_artifact(result)
        if isinstance(artifact.get("internal_model_usage"), dict):
            payload["internal_model_usage"] = artifact["internal_model_usage"]
        if isinstance(artifact.get("internal_model_calls"), list):
            payload["internal_model_calls"] = artifact["internal_model_calls"]
        return (AgentEventType.TOOL_CALL_COMPLETED, payload)

    def map_tool_call_failed(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        error: Any,
        metadata: Any = None,
        attribution: dict[str, str] | None = None,
    ) -> tuple[AgentEventType, dict[str, Any]]:
        return (
            AgentEventType.TOOL_CALL_FAILED,
            {
                "id": call_id,
                "name": name,
                "arguments": _serialize(arguments),
                "error": _serialize(error),
                **(attribution or _agent_attribution(metadata, self._subagent_names)),
            },
        )

    def map_final_answer(self, result: Any) -> str:
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, list) and messages:
                selected = _select_final_answer_text(messages)
                if selected:
                    return selected
            for key in ("final_response", "answer", "content"):
                if result.get(key) is not None:
                    return str(result[key])
        return _extract_text(result) or str(result)

    def map_final_reasoning(self, result: Any) -> str:
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    reasoning = _extract_reasoning(message)
                    if reasoning:
                        return reasoning
            choices = result.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    reasoning = _extract_reasoning(choice.get("message"))
                    if reasoning:
                        return reasoning
            reasoning = _extract_reasoning(result)
            if reasoning:
                return reasoning
        return _extract_reasoning(result)

    def map_final_tool_calls(self, result: Any) -> list[ToolCallRecord]:
        if not isinstance(result, dict):
            return []
        messages = result.get("messages")
        if not isinstance(messages, list):
            return []
        messages = _messages_after_last_user_message(messages)

        calls: dict[str, ToolCallRecord] = {}
        order: list[str] = []
        for message in messages:
            for tool_call in _message_tool_calls(message):
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
                if not call_id:
                    continue
                if call_id not in calls:
                    order.append(call_id)
                calls[call_id] = ToolCallRecord(
                    id=call_id,
                    name=str(tool_call.get("name") or ""),
                    arguments=_serialize(tool_call.get("args")),
                )

            tool_call_id = _message_tool_call_id(message)
            if tool_call_id and tool_call_id in calls:
                calls[tool_call_id].result = _message_tool_result(message)

        return [calls[call_id] for call_id in order if call_id in calls]

    def map_deepagent_event(
        self, event: dict[str, Any]
    ) -> tuple[AgentEventType, dict[str, Any]] | None:
        mapped_structured_event = self._map_structured_event(event)
        if mapped_structured_event is not None:
            return mapped_structured_event
        event_name = event.get("event")
        data = event.get("data")
        metadata = event.get("metadata")
        if event_name in {"on_chain_stream", "on_chain_end"}:
            mapped_compaction_event = _map_context_compaction_from_payload(data)
            if mapped_compaction_event is not None:
                return mapped_compaction_event
        if event_name == "on_chat_model_start":
            if _is_summarization_metadata(metadata):
                return None
            return (
                AgentEventType.MODEL_CALL_STARTED,
                {
                    "name": str(event.get("name") or ""),
                    "trace_run_id": str(event.get("run_id") or ""),
                },
            )
        if event_name == "on_chat_model_stream" and isinstance(data, dict):
            if _is_summarization_metadata(metadata) or self.is_subagent_event(event):
                return None
            chunk = data.get("chunk")
            return self.map_stream_reasoning(chunk) or self.map_stream_delta(chunk)
        if event_name == "on_tool_start" and isinstance(data, dict):
            self._suppressing_internal_tool_call_markup = False
            call_id = _event_tool_call_id(event, data, metadata)
            name = str(event.get("name") or "")
            arguments = _tool_event_arguments(data.get("input"))
            return self.map_tool_call_started(
                call_id,
                name,
                arguments,
                metadata,
                attribution=self._tool_attribution(event, name, arguments),
            )
        if event_name == "on_tool_end" and isinstance(data, dict):
            call_id = _event_tool_call_id(event, data, metadata)
            name = str(event.get("name") or "")
            arguments = data.get("input")
            if arguments is None and isinstance(metadata, dict):
                arguments = metadata.get("input")
            tool_arguments = _tool_event_arguments(arguments)
            return self.map_tool_call_completed(
                call_id,
                name,
                tool_arguments,
                data.get("output"),
                metadata,
                attribution=self._tool_attribution(event, name, tool_arguments, closing=True),
            )
        if event_name == "on_tool_error" and isinstance(data, dict):
            call_id = _event_tool_call_id(event, data, metadata)
            name = str(event.get("name") or "")
            arguments = data.get("input")
            if arguments is None and isinstance(metadata, dict):
                arguments = metadata.get("input")
            tool_arguments = _tool_event_arguments(arguments)
            return self.map_tool_call_failed(
                call_id,
                name,
                tool_arguments,
                data.get("error"),
                metadata,
                attribution=self._tool_attribution(event, name, tool_arguments, closing=True),
            )
        return None

    def _map_structured_event(
        self, event: dict[str, Any]
    ) -> tuple[AgentEventType, dict[str, Any]] | None:
        if event.get("method") == "values":
            return _map_context_compaction_from_payload(event.get("params"))
        if event.get("method") != "messages":
            return None
        params = event.get("params")
        if not isinstance(params, dict):
            return None
        data = params.get("data")
        if not isinstance(data, (list, tuple)) or not data:
            return None
        message_event = data[0]
        if not isinstance(message_event, dict):
            return None
        if message_event.get("event") != "content-block-delta":
            return None
        delta = message_event.get("delta")
        if not isinstance(delta, dict):
            return None
        reasoning = _extract_reasoning(delta)
        if reasoning:
            if self._should_suppress_internal_tool_call_markup(reasoning):
                return None
            return (
                AgentEventType.REASONING_DELTA,
                {"text": reasoning, "format": "reasoning_content"},
            )
        text = delta.get("text")
        if not text:
            return None
        if self._should_suppress_internal_tool_call_markup(str(text)):
            return None
        return AgentEventType.MESSAGE_DELTA, {"text": str(text)}

    def _should_suppress_internal_tool_call_markup(self, text: str) -> bool:
        starts_markup = is_tool_call_markup_start(text)
        contains_markup = is_tool_call_markup_fragment(text)
        if starts_markup:
            self._suppressing_internal_tool_call_markup = True
        suppress = self._suppressing_internal_tool_call_markup or contains_markup
        if is_tool_call_markup_end(text):
            self._suppressing_internal_tool_call_markup = False
        return suppress


class _LazyCitationPolicyClassifier:
    def __init__(self, factory: Callable[[], CitationPolicyClassifier]) -> None:
        self._factory = factory
        self._classifier: CitationPolicyClassifier | None = None

    def classify_policy(self, request: Any):
        if self._classifier is None:
            self._classifier = self._factory()
        return self._classifier.classify_policy(request)


class _LazyClaimReviewer:
    def __init__(self, factory: Callable[[], AnswerReviewClaimReviewer]) -> None:
        self._factory = factory
        self._reviewer: AnswerReviewClaimReviewer | None = None

    def review_claims(self, request: Any):
        if self._reviewer is None:
            self._reviewer = self._factory()
        return self._reviewer.review_claims(request)


class _LazySemanticReviewer:
    def __init__(self, factory: Callable[[], AnswerReviewSemanticReviewer]) -> None:
        self._factory = factory
        self._reviewer: AnswerReviewSemanticReviewer | None = None

    def review_semantics(self, request: Any):
        if self._reviewer is None:
            self._reviewer = self._factory()
        return self._reviewer.review_semantics(request)


class DeepAgentsRuntime(AgentRuntime):
    """Run ViKnow chat turns through DeepAgents."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        profile: AgentProfile | None = None,
        model_config: AgentModelConfig | None = None,
        config: DeepAgentsKernelConfig | None = None,
        tools: Sequence[Any] = (),
        agent_factory: Callable[..., Any] | None = None,
        model_factory: Callable[[AgentModelConfig], Any] | None = None,
        event_mapper: DeepAgentsEventMapper | None = None,
        event_store: AgentEventStore | None = None,
        skills: Sequence[SkillSpec] = (),
        langchain_callbacks: Sequence[Any] = (),
        answer_reviewer: AnswerReviewer | None = None,
        answer_review_policy_classifier: CitationPolicyClassifier | None = None,
        answer_review_claim_reviewer: AnswerReviewClaimReviewer | None = None,
        answer_review_semantic_reviewer: AnswerReviewSemanticReviewer | None = None,
        answer_review_model_config: ToolModelConfig | None = None,
        answer_review_claim_relevance_scorer: AnswerReviewClaimRelevanceScorer | None = None,
        stop_gate_judge: StopGateJudge | None = None,
        model_options: Sequence[AgentModelOption] = (),
        model_providers: dict[str, ModelProviderConfig] | None = None,
        user_memory_service: UserMemoryService | None = None,
        scheduler: ResourceScheduler | None = None,
        filesystem_manager: Any | None = None,
        precompiled_knowledge_loader: Callable[..., Awaitable[str]] | None = None,
        usage_push_config: LangfuseUsagePushConfig | None = None,
        langfuse_binding: LangfuseRunBindingConfig | None = None,
        langfuse_binding_provider: Callable[[], LangfuseRunBindingConfig] | None = None,
        subagent_specs: Sequence[SubAgentSpec] = (),
        subagent_skills: Sequence[SkillSpec] = (),
        behavior_snapshot_factory: Callable[[AgentOp], RunBehaviorSnapshot] | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.profile = profile or AgentProfile(
            id="default",
            label="Default",
            description="Default DeepAgents runtime profile",
        )
        self.model_config = model_config or AgentModelConfig()
        self.config = self._resolve_config(config or DeepAgentsKernelConfig())
        self.tools = list(tools)
        self.skills = list(skills)
        self.model_options = list(model_options)
        self.model_providers = dict(model_providers or {})
        self._model_connections: dict = {}
        self._agent_factory = agent_factory
        self._model_factory = model_factory
        self.event_mapper = event_mapper or DeepAgentsEventMapper()
        self.event_mapper.bind_subagent_names(spec.name for spec in subagent_specs)
        self.event_mapper.bind_tool_owners(exclusive_specialist_tool_owners(subagent_specs))
        self.event_store = event_store or InMemoryAgentEventStore()
        self.langchain_callbacks = list(langchain_callbacks)
        self.answer_reviewer = answer_reviewer
        self.answer_review_policy_classifier = answer_review_policy_classifier
        self.answer_review_claim_reviewer = answer_review_claim_reviewer
        self.answer_review_semantic_reviewer = answer_review_semantic_reviewer
        self.answer_review_model_config = answer_review_model_config
        self.answer_review_claim_relevance_scorer = answer_review_claim_relevance_scorer
        self.stop_gate_judge = stop_gate_judge
        from agents.runtime.core.memory import NoOpUserMemoryService

        self.user_memory_service = user_memory_service or NoOpUserMemoryService()
        self.filesystem_manager = filesystem_manager
        self._checkpoints = build_checkpointer_provider(self.config.checkpointer)
        self._runs: dict[str, _RunState] = {}
        self._shutdown = False
        self._scheduler = scheduler
        self._precompiled_knowledge_loader = precompiled_knowledge_loader
        self.usage_push_config = usage_push_config
        self.langfuse_binding = langfuse_binding or LangfuseRunBindingConfig()
        self.langfuse_binding_provider = langfuse_binding_provider
        self.subagent_specs = tuple(subagent_specs)
        self._subagent_skills = {skill.name: skill for skill in (*skills, *subagent_skills)}
        self._behavior_snapshot_factory = behavior_snapshot_factory

    @property
    def scheduler(self) -> ResourceScheduler | None:
        return self._scheduler

    @asynccontextmanager
    async def _run_source_context(
        self,
        op: AgentOp,
        state: _RunState,
        run_id: str,
        session_id: str,
        *,
        precompiled_knowledge_context: str = "",
    ):
        from agents.runtime.core.scheduler.metrics_domain import utc_now
        from agents.runtime.core.scheduler.metrics_events import record_run_finished, record_run_started

        started_at = utc_now()
        profile_id = self.profile.id
        if self._scheduler is not None:
            self._scheduler.register_source(run_id)
        record_run_started(run_id=run_id, profile_id=profile_id, started_at=started_at)
        set_source_id(run_id)
        tool_context_token = set_tool_run_context(
            ToolRunContext(
                run_id=run_id,
                user_id=op.user_id,
                workspace_id=op.workspace_id or "",
                session_id=session_id,
                allowed_library_ids=(None if op.library_ids is None else frozenset(op.library_ids)),
                precompiled_knowledge_context=precompiled_knowledge_context,
                oracle_scope=op.oracle_scope,
                oracle_task=op.oracle_task,
                original_question=_run_original_question(state) or op.input or "",
                original_question_factory=lambda: _run_original_question(state),
                answer_slots_factory=lambda: [
                    {
                        **slot,
                        "evidence_state": state.evidence_ledger.slot_evidence.get(slot["id"], {}),
                    }
                    for slot in _run_answer_slots(state)
                ],
                remaining_seconds_factory=lambda: (
                    0.0
                    if state.budget_exhausted
                    or state.status
                    in {
                        AgentStatusType.COMPLETED,
                        AgentStatusType.FAILED,
                        AgentStatusType.INTERRUPTED,
                        AgentStatusType.SHUTDOWN,
                    }
                    else self.profile.runtime_budget.time_limit_seconds
                    - (time.monotonic() - state.budget_started_at)
                    if self.profile.runtime_budget.enabled
                    else None
                ),
                tool_gate_context_factory=lambda: ToolGateContext(
                    selected_library_ids=set(op.library_ids or ())
                    | {asset.library_id for asset in op.selected_knowledge_assets},
                    selected_asset_ids={asset.asset_id for asset in op.selected_knowledge_assets},
                    enforce_selected_asset_scope=self.profile.oracle_collection,
                    oracle_collection=self.profile.oracle_collection,
                    completed_tool_calls=list(state.completed_tool_calls.values()),
                    ledger=state.evidence_ledger,
                    session_ledger=state.session_evidence_ledger,
                    budget_warning=state.budget_warning_emitted,
                    targeted_repair=state.targeted_repair_started,
                ),
            )
        )
        status = "completed"
        try:
            yield
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            reset_tool_run_context(tool_context_token)
            set_source_id(None)
            if self._scheduler is not None:
                self._scheduler.deregister_source(run_id)
            record_run_finished(
                run_id=run_id,
                profile_id=profile_id,
                started_at=started_at,
                status=status,
            )

    async def submit(self, op: AgentOp) -> SubmitResult:
        if self._shutdown:
            raise RuntimeError("agent runtime is shut down")
        if op.type == AgentOpType.USER_INPUT:
            if not op.input:
                raise ValueError("user_input operation requires input")
            run_id = op.run_id or str(uuid4())
            session_id = op.session_id or run_id
            state = _RunState(
                run_id=run_id,
                session_id=session_id,
                thread_id=_thread_id(op.user_id, op.workspace_id, session_id),
                status=AgentStatusType.RUNNING,
                langfuse_trace_name=op.langfuse_trace_name,
                original_question=op.input or "",
                original_op=op,
            )
            self._runs[run_id] = state
            state.task = asyncio.create_task(self._execute_user_input(op, state))
            return SubmitResult(run_id=run_id, session_id=session_id)
        if op.type == AgentOpType.INTERRUPT:
            if not op.run_id:
                raise ValueError("interrupt operation requires run_id")
            state = self._runs.get(op.run_id)
            if state is None:
                raise KeyError(op.run_id)
            if state.status != AgentStatusType.RUNNING:
                return SubmitResult(run_id=op.run_id, session_id=state.session_id)
            state.status = AgentStatusType.INTERRUPTED
            if state.task is not None and not state.task.done():
                state.task.cancel()
            await self._emit_open_tool_call_failures(state, message="run interrupted")
            await self._emit_streamed_snapshots(state)
            await self._emit(
                state,
                AgentEventType.RUN_INTERRUPTED,
                self._terminal_payload(state, {"message": "run interrupted"}),
            )
            return SubmitResult(run_id=op.run_id, session_id=state.session_id)
        if op.type == AgentOpType.SHUTDOWN:
            await self.aclose()
            return SubmitResult(run_id=op.run_id or str(uuid4()), session_id=op.session_id)
        raise ValueError(f"unsupported agent operation: {op.type}")

    async def aclose(self) -> None:
        """Mark runtime shut down and close the checkpointer provider."""

        if self._shutdown:
            return
        self._shutdown = True
        if self._scheduler is not None:
            for state in list(self._runs.values()):
                if state.status == AgentStatusType.RUNNING:
                    with suppress(Exception):
                        self._scheduler.deregister_source(state.run_id)
        try:
            if self._checkpoints is not None:
                await self._checkpoints.aclose()
        finally:
            await close_model_connections(self._model_connections)

    async def next_event(self, run_id: str | None = None) -> AgentEvent:
        return await self._resolve_run(run_id).queue.get()

    async def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        self._resolve_run(run_id)
        return self.event_store.events_after(run_id, after_sequence)

    async def get_status(self, run_id: str | None = None) -> AgentStatus:
        if run_id is None:
            if self._shutdown:
                return AgentStatus(status=AgentStatusType.SHUTDOWN)
            active = [
                state for state in self._runs.values() if state.status == AgentStatusType.RUNNING
            ]
            if not active:
                return AgentStatus(status=AgentStatusType.IDLE)
            return AgentStatus(
                status=AgentStatusType.RUNNING,
                run_id=active[-1].run_id,
                session_id=active[-1].session_id,
            )
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return AgentStatus(
            status=state.status,
            run_id=state.run_id,
            session_id=state.session_id,
            message=state.message,
        )

    async def _execute_user_input(self, op: AgentOp, state: _RunState) -> None:
        entry = "eval" if op.trace_capture == TraceCaptureMode.FULL else "chat"
        async with start_run_span(
            run_id=state.run_id,
            session_id=state.session_id,
            entry=entry,
            profile_id=self.profile.id,
        ):
            await self._execute_user_input_pipeline(op, state)

    async def _execute_user_input_pipeline(self, op: AgentOp, state: _RunState) -> None:
        precompiled_knowledge_context = ""
        if op.use_precompiled_knowledge:
            precompiled_knowledge_context = await self._load_precompiled_knowledge_context(op)
        async with self._run_source_context(
            op,
            state,
            state.run_id,
            state.session_id,
            precompiled_knowledge_context=precompiled_knowledge_context,
        ):
            started_payload: dict[str, Any] = {
                "session_id": state.session_id,
                "run_kind": (
                    "evaluation" if op.trace_capture != TraceCaptureMode.NONE else "production"
                ),
                "enabled_tools": list(self.profile.enabled_tools),
                "enabled_skills": list(self.profile.enabled_skills),
            }
            if op.selected_knowledge_assets:
                started_payload["selected_knowledge_assets"] = [
                    asset.model_dump(mode="json") for asset in op.selected_knowledge_assets
                ]
            if op.library_ids is not None:
                started_payload["library_ids"] = list(op.library_ids)
            if self._behavior_snapshot_factory is not None:
                snapshot = self._behavior_snapshot_factory(op)
                effective_model = self._resolve_model_provider(self._model_config_for_op(op))
                snapshot = snapshot.model_copy(
                    update={
                        "agent_model": build_model_snapshot(
                            effective_model,
                            requested=op.agent_model,
                        )
                    }
                )
                started_payload["behavior_snapshot"] = snapshot.model_dump(mode="json")
            await self._emit(state, AgentEventType.RUN_STARTED, started_payload)
            await self._emit(
                state,
                AgentEventType.USER_MESSAGE,
                {
                    "text": op.input or "",
                    "session_id": state.session_id,
                    "user_id": op.user_id,
                    "workspace_id": op.workspace_id,
                    "attachments": self._user_message_attachments(op),
                },
            )
            budget_watchdog = asyncio.create_task(self._budget_watchdog(state))
            abort_token = _RUN_BUDGET_ABORT.set(state.budget_abort)
            try:
                try:
                    result = await self._run_deepagent(
                        op,
                        state,
                        precompiled_knowledge_context=precompiled_knowledge_context,
                    )
                except asyncio.CancelledError:
                    if state.status in {
                        AgentStatusType.INTERRUPTED,
                        AgentStatusType.FAILED,
                    }:
                        return
                    raise
                except Exception as exc:
                    if state.status != AgentStatusType.RUNNING:
                        return
                    await self._emit_open_tool_call_failures(state, message=str(exc))
                    from agents.runtime.core.model_gateway_recovery import terminal_model_failure

                    termination_reason = (
                        "model_exception" if terminal_model_failure(exc) else "runtime_exception"
                    )
                    if await self._try_publish_choice_fallback(
                        op, state, termination_reason=termination_reason, error=str(exc)
                    ):
                        return
                    state.status = AgentStatusType.FAILED
                    state.message = str(exc)
                    await self._emit_streamed_snapshots(state)
                    await self._emit(
                        state,
                        AgentEventType.RUN_FAILED,
                        self._terminal_payload(state, {"message": str(exc)}),
                    )
                    return
                if state.status != AgentStatusType.RUNNING:
                    return
                await self._emit_final_tool_call_events(state, result)
                await self._emit_open_tool_call_failures(
                    state,
                    message="tool call did not complete before run finished",
                )

                final_reasoning = self.event_mapper.map_final_reasoning(result)
                if final_reasoning and not state.streamed_reasoning:
                    state.streamed_reasoning = final_reasoning
                    await self._emit(
                        state,
                        AgentEventType.REASONING_DELTA,
                        {"text": final_reasoning, "format": "reasoning_content"},
                    )
                if state.streamed_reasoning:
                    await self._emit(
                        state,
                        AgentEventType.REASONING_COMPLETED,
                        {"text": state.streamed_reasoning, "format": "reasoning_content"},
                    )

                answer = self.event_mapper.map_final_answer(result)
                await self._review_and_publish_answer(op, state, answer, result)
                if state.status == AgentStatusType.COMPLETED and op.memory_enabled:
                    self.user_memory_service.schedule_sync_turn(
                        user_id=op.user_id,
                        user_input=op.input or "",
                        assistant_answer=answer or state.streamed_text,
                        session_id=state.session_id,
                    )
            except asyncio.CancelledError:
                if state.status in {
                    AgentStatusType.INTERRUPTED,
                    AgentStatusType.FAILED,
                }:
                    return
                raise
            finally:
                _RUN_BUDGET_ABORT.reset(abort_token)
                budget_watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await budget_watchdog

    async def _emit_final_tool_call_events(self, state: _RunState, result: Any) -> None:
        final_tool_calls = self.event_mapper.map_final_tool_calls(result)
        for tool_call in final_tool_calls:
            tool_call_id = _mapped_tool_call_id(state, tool_call.id)
            if (
                tool_call.id in state.started_tool_call_ids
                or tool_call_id in state.started_tool_call_ids
            ):
                continue
            await self._emit(
                state,
                AgentEventType.TOOL_CALL_STARTED,
                {
                    "id": tool_call_id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
            )
            state.started_tool_call_ids.add(tool_call.id)
            state.started_tool_call_ids.add(tool_call_id)

        for tool_call in final_tool_calls:
            tool_call_id = _mapped_tool_call_id(state, tool_call.id)
            if (
                tool_call.id in state.completed_tool_call_ids
                or tool_call_id in state.completed_tool_call_ids
                or tool_call.id in state.failed_tool_call_ids
                or tool_call_id in state.failed_tool_call_ids
            ):
                continue
            tool_call_to_store = tool_call
            result_with_citations = _assign_completed_tool_result_citations(
                state,
                tool_call_to_store.name,
                tool_call_to_store.result,
                tool_call_id=tool_call_id,
            )
            if result_with_citations is not tool_call_to_store.result:
                tool_call_to_store = tool_call_to_store.model_copy(
                    update={"result": result_with_citations}
                )
            completed_payload: dict[str, Any] = {
                "id": tool_call_id,
                "name": tool_call_to_store.name,
                "arguments": tool_call_to_store.arguments,
                "result": tool_call_to_store.result,
            }
            await self._emit(
                state,
                AgentEventType.TOOL_CALL_COMPLETED,
                completed_payload,
            )
            state.completed_tool_call_ids.add(tool_call.id)
            state.completed_tool_call_ids.add(tool_call_id)
            _remember_completed_tool_call(state, tool_call_id, tool_call_to_store)

    async def _emit_open_tool_call_failures(
        self,
        state: _RunState,
        *,
        message: str,
    ) -> None:
        """Close any started-but-unfinished tool cards (interrupt / crash / hang)."""
        open_ids = [
            call_id
            for call_id in state.started_tool_call_ids
            if call_id not in state.completed_tool_call_ids
            and call_id not in state.failed_tool_call_ids
        ]
        emitted: set[str] = set()
        for call_id in open_ids:
            canonical = _mapped_tool_call_id(state, call_id)
            if canonical in emitted:
                continue
            if (
                canonical in state.completed_tool_call_ids
                or canonical in state.failed_tool_call_ids
            ):
                continue
            emitted.add(canonical)
            await self._emit(
                state,
                AgentEventType.TOOL_CALL_FAILED,
                {
                    "id": canonical,
                    "name": "",
                    "arguments": "",
                    "error": message,
                },
            )
            state.failed_tool_call_ids.add(canonical)
            state.failed_tool_call_ids.add(call_id)

    async def _review_and_publish_answer(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
        result: Any,
    ) -> None:
        if state.status != AgentStatusType.RUNNING:
            return
        await self._flush_pending_candidate_text(state)
        current_answer = answer or state.streamed_text
        if has_final_choice(_run_original_question(state), current_answer):
            state.best_effort_answer = current_answer
        current_result = result
        if not has_final_choice(
            _run_original_question(state), current_answer
        ) and await self._try_publish_choice_fallback(
            op, state, termination_reason="missing_final_choice", draft=current_answer
        ):
            return
        current_answer, current_result, citation_context, _ = await self._apply_stop_gate(
            op,
            state,
            current_answer,
            current_result,
        )
        if not self._answer_review_enabled():
            await self._publish_final_answer(
                op,
                state,
                current_answer,
                current_result,
                citation_context=citation_context,
            )
            return

        async with start_span("answer_review", **{"agents.runtime.phase": "answer_review"}):
            await self._run_answer_review_loop(
                op,
                state,
                current_answer,
                current_result,
                citation_context,
            )

    async def _run_answer_review_loop(
        self,
        op: AgentOp,
        state: _RunState,
        current_answer: str,
        current_result: Any,
        citation_context: Any,
    ) -> None:
        review_config = self.profile.answer_review
        targeted = review_config.targeted_repair_enabled
        max_attempts = (
            2
            if targeted
            else max(1, review_config.max_retries + 1)
            if review_config.gate_enabled
            else 1
        )
        attempt = 1
        previous_feedback: str | None = None
        final_review: AnswerReviewResult | None = None

        while True:
            if attempt > 1:
                citation_context = self._final_answer_citation_context(
                    op, state, current_answer, use_policy_classifier=False
                )
            if has_final_choice(_run_original_question(state), current_answer):
                state.best_effort_answer = current_answer
            await self._emit(
                state,
                AgentEventType.ANSWER_REVIEW_STARTED,
                {"attempt": attempt, "max_attempts": max_attempts},
            )
            attempt_started_at = time.perf_counter()
            request = self._review_request(
                op,
                state,
                citation_context,
                attempt=attempt,
                previous_feedback=previous_feedback,
            )
            _log_answer_review_stage(
                state,
                "started",
                attempt=attempt,
                max_attempts=max_attempts,
                answer_chars=len(current_answer or ""),
            )
            try:
                if await self._maybe_auto_visual_ground_counting_evidence(op, state):
                    citation_context = self._final_answer_citation_context(
                        op, state, current_answer, use_policy_classifier=False
                    )
                from agents.runtime.core.tools.context import scoped_model_deadline

                with scoped_model_deadline(review_config.timeout_seconds):
                    async with asyncio.timeout(review_config.timeout_seconds):
                        citation_context = await asyncio.to_thread(
                            self._with_answer_review_citation_policy,
                            op,
                            state,
                            citation_context,
                        )
                        _log_answer_review_stage(
                            state,
                            "citation_context_ready",
                            attempt=attempt,
                            citations=len(citation_context.review_citations),
                            citation_errors=len(citation_context.citation_errors),
                            current_claims=len(citation_context.current_claims),
                            historical_claims=len(state.historical_claims),
                            citation_policy=citation_context.citation_policy.mode,
                        )
                        request = self._review_request(
                            op,
                            state,
                            citation_context,
                            attempt=attempt,
                            previous_feedback=previous_feedback,
                        )
                        _log_answer_review_stage(
                            state,
                            "request_ready",
                            attempt=attempt,
                            evidence=len(request.evidence),
                            citations=len(request.citations),
                            citation_errors=len(request.citation_errors),
                            tools=len(request.citation_policy.tool_names),
                        )
                        _log_answer_review_stage(state, "reviewer_start", attempt=attempt)
                        final_review = await asyncio.to_thread(
                            self._answer_reviewer(op).review,
                            request,
                        )
                review_execution_failed = (
                    review_config.distinguish_execution_failures
                    and final_review.execution_status
                    in {"unavailable", "invalid_input", "invalid_output"}
                )
                if review_execution_failed:
                    final_review = final_review.model_copy(
                        update={"passed": False, "retryable": False}
                    )
                if targeted and not review_execution_failed:
                    assessed = {item.slot_id for item in final_review.slot_assessments}
                    for slot in _run_answer_slots(state):
                        if slot["id"] not in assessed:
                            state.evidence_ledger.slot_evidence[slot["id"]] = {
                                "slot_id": slot["id"],
                                "status": "unknown",
                                "unresolved_gaps": ["missing_slot_assessment"],
                                "claim_refs": [],
                            }
                            final_review.risks.append(
                                AnswerReviewRisk(
                                    id="unassessed-" + slot["id"],
                                    severity=RiskSeverity.MEDIUM,
                                    kind="missing_slot_assessment",
                                    message="An original question condition was not reviewed.",
                                    slot_ids=[slot["id"]],
                                    missing_evidence=["missing_slot_assessment"],
                                    repair_action="revise_answer",
                                )
                            )
                for assessment in [] if review_execution_failed else final_review.slot_assessments:
                    slot = next(
                        (
                            slot
                            for slot in _run_answer_slots(state)
                            if slot["id"] == assessment.slot_id
                        ),
                        None,
                    )
                    if slot is None:
                        continue
                    coverage = resolve_slot_assessment(
                        assessment,
                        {**slot, "required_modalities": []} if state.answer_plan else slot,
                        _review_evidence_facts(state),
                    )
                    state.evidence_ledger.slot_evidence[assessment.slot_id] = coverage
                    if coverage["unresolved_gaps"]:
                        final_review.risks.append(
                            AnswerReviewRisk(
                                id="slot-" + assessment.slot_id,
                                kind="incomplete_slot_evidence",
                                severity=RiskSeverity.MEDIUM,
                                message="; ".join(coverage["unresolved_gaps"]),
                                slot_ids=[assessment.slot_id],
                                missing_evidence=coverage["unresolved_gaps"],
                                conflicting_claim_refs=coverage["conflicting_claim_refs"],
                                repair_action="locate_adjacent"
                                if coverage["missing_ranges"]
                                else "inspect",
                            )
                        )
                _log_answer_review_stage(
                    state,
                    "reviewer_completed",
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(attempt_started_at),
                    passed=final_review.passed,
                    overall_score=final_review.overall_score,
                    citation_validity=final_review.dimension_scores.citation_validity,
                    claim_support=final_review.dimension_scores.claim_support,
                    contradiction=final_review.dimension_scores.contradiction,
                    coverage=final_review.dimension_scores.coverage,
                )
            except TimeoutError:
                timeout_seconds = review_config.timeout_seconds
                safe_message = f"answer review exceeded its {timeout_seconds:g} second deadline"
                _log_answer_review_stage(
                    state,
                    "timed_out",
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(attempt_started_at),
                    error=safe_message,
                    level="warning",
                )
                await self._emit(
                    state,
                    AgentEventType.ANSWER_REVIEW_COMPLETED,
                    {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "passed": False,
                        "retryable": True,
                        "error": {
                            "code": "answer_review_timeout",
                            "kind": "timeout",
                            "retryable": True,
                            "safe_message": safe_message,
                        },
                        "review_input": self._answer_review_replay_input(op, request).model_dump(
                            mode="json"
                        ),
                    },
                )
                if review_config.distinguish_execution_failures:
                    _record_review_execution_failure(state, "unavailable", attempt)
                    current_answer = (
                        unverified_choice_answer(
                            _run_original_question(state) or op.input or "", current_answer
                        )
                        or current_answer
                    )
                    citation_context = self._final_answer_citation_context(
                        op, state, current_answer, use_policy_classifier=False
                    )
                if review_config.allow_low_confidence_output:
                    await self._publish_final_answer(
                        op,
                        state,
                        current_answer,
                        current_result,
                        citation_context=citation_context,
                    )
                else:
                    await self._fail_answer_review(state)
                return
            except Exception as exc:
                _log_answer_review_stage(
                    state,
                    "failed",
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(attempt_started_at),
                    error=str(exc),
                    level="warning",
                )
                await self._emit(
                    state,
                    AgentEventType.ANSWER_REVIEW_COMPLETED,
                    {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "passed": False,
                        "retryable": False,
                        "error": {"message": str(exc)},
                        "review_input": self._answer_review_replay_input(op, request).model_dump(
                            mode="json"
                        ),
                    },
                )
                if review_config.gate_enabled and not review_config.allow_low_confidence_output:
                    await self._fail_answer_review(state)
                    return
                if review_config.distinguish_execution_failures:
                    _record_review_execution_failure(state, "unavailable", attempt)
                    current_answer = (
                        unverified_choice_answer(
                            _run_original_question(state) or op.input or "", current_answer
                        )
                        or current_answer
                    )
                    citation_context = self._final_answer_citation_context(
                        op, state, current_answer, use_policy_classifier=False
                    )
                if targeted and not review_config.distinguish_execution_failures:
                    for slot in _run_answer_slots(state):
                        state.evidence_ledger.slot_evidence[slot["id"]] = {
                            "slot_id": slot["id"],
                            "status": "unknown",
                            "unresolved_gaps": ["review_failed"],
                            "claim_refs": [],
                        }
                    state.repair_diagnostics.append(
                        {
                            "action": "review_failed_unverified_choice",
                            "error_type": type(exc).__name__,
                            "attempt": attempt,
                        }
                    )
                    fallback = unverified_choice_answer(
                        _run_original_question(state) or op.input or "", current_answer
                    )
                    if fallback:
                        current_answer = fallback
                await self._publish_final_answer(
                    op,
                    state,
                    current_answer,
                    current_result,
                    citation_context=citation_context
                    if review_config.distinguish_execution_failures
                    else None,
                )
                return

            await self._emit(
                state,
                AgentEventType.ANSWER_REVIEW_COMPLETED,
                {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "passed": final_review.passed,
                    "retryable": final_review.retryable,
                    "result": final_review.model_dump(mode="json"),
                    "review_input": self._answer_review_replay_input(op, request).model_dump(
                        mode="json"
                    ),
                },
            )
            _log_answer_review_stage(
                state,
                "completed",
                attempt=attempt,
                elapsed_ms=_elapsed_ms(attempt_started_at),
                passed=final_review.passed,
                retryable=final_review.retryable,
            )
            self._update_answer_review_trace_metadata(
                state,
                final_review,
                event_sequence=state.sequence,
            )
            if review_execution_failed:
                _record_review_execution_failure(state, final_review.execution_status, attempt)
                if review_config.gate_enabled and not review_config.allow_low_confidence_output:
                    await self._fail_answer_review(state)
                    return
                current_answer = (
                    unverified_choice_answer(
                        _run_original_question(state) or op.input or "", current_answer
                    )
                    or current_answer
                )
                await self._publish_final_answer(
                    op, state, current_answer, current_result, answer_review=final_review
                )
                return
            repair_risks = evidence_repair_risks(final_review) if targeted else []
            if targeted:
                state.repair_diagnostics.append(
                    {
                        "attempt": attempt,
                        "missing": [
                            risk.model_dump(mode="json")
                            for risk in final_review.risks
                            if risk.missing_evidence or risk.conflicting_claim_refs
                        ],
                        "action": "targeted_inspect"
                        if repair_risks and attempt == 1
                        else "publish",
                        "claim_refs": [claim.claim_ref for claim in state.evidence_ledger.claims],
                        "selected_choice": final_review.selected_choice.model_dump(mode="json")
                        if final_review.selected_choice
                        else None,
                    }
                )
            if final_review.passed and not repair_risks:
                await self._publish_final_answer(
                    op,
                    state,
                    current_answer,
                    current_result,
                    answer_review=final_review,
                    citation_context=citation_context,
                )
                return
            if (not review_config.gate_enabled and not targeted) or (targeted and not repair_risks):
                if review_config.gate_enabled and not review_config.allow_low_confidence_output:
                    await self._fail_answer_review(state)
                    return
                await self._publish_final_answer(
                    op,
                    state,
                    current_answer,
                    current_result,
                    answer_review=final_review,
                    citation_context=citation_context,
                )
                return
            if (
                state.budget_finalizing
                or (not final_review.retryable and not repair_risks)
                or attempt >= max_attempts
            ):
                if review_config.allow_low_confidence_output:
                    await self._publish_final_answer(
                        op,
                        state,
                        current_answer,
                        current_result,
                        answer_review=final_review,
                        citation_context=citation_context,
                    )
                    return
                await self._fail_answer_review(state)
                return

            previous_feedback = final_review.retry_instruction or final_review.summary
            await self._emit(
                state,
                AgentEventType.ANSWER_RETRY_STARTED,
                {
                    "retry_attempt": attempt,
                    "next_attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "reason": previous_feedback,
                },
            )
            state.retry_instruction = self._build_retry_prompt(
                _run_original_question(state) or op.input or "",
                current_answer,
                final_review,
                evidence_context=_visual_ground_retry_context(
                    citation_context.current_claims if citation_context is not None else []
                ),
            )
            if has_final_choice(_run_original_question(state), current_answer):
                state.best_effort_answer = current_answer
            if self.profile.runtime_budget.enabled:
                await self._evaluate_runtime_budget(state)
                if state.status != AgentStatusType.RUNNING:
                    return
            if targeted:
                state.targeted_repair_started = True
                state.retry_instruction = targeted_retry_prompt(
                    _run_original_question(state) or op.input or "",
                    current_answer,
                    repair_risks,
                )
            state.streamed_text = ""
            current_result = await self._run_deepagent(op, state)
            await self._emit_final_tool_call_events(state, current_result)
            current_answer = (
                self.event_mapper.map_final_answer(current_result) or state.streamed_text
            )
            attempt += 1

    async def _apply_stop_gate(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
        result: Any,
    ) -> tuple[str, Any, _FinalAnswerCitationContext, bool]:
        async with start_span("stop_gate", **{"agents.runtime.phase": "stop_gate"}):
            return await self._evaluate_stop_gate(op, state, answer, result)

    async def _evaluate_stop_gate(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
        result: Any,
    ) -> tuple[str, Any, _FinalAnswerCitationContext, bool]:
        citation_context = self._final_answer_citation_context(
            op, state, answer, use_policy_classifier=False
        )
        if not self._stop_gate_active() or state.stop_gate_middleware_observed:
            return answer, result, citation_context, True
        if state.stop_gate is None:
            state.stop_gate = StopCandidateGate(judge=self._stop_gate_judge(op))
        decision = state.stop_gate.evaluate(
            StopGateInput(
                question=op.input or "",
                answer=answer,
                citation_policy=citation_context.citation_policy,
                resolved_citation_count=len(citation_context.review_citations),
                citation_errors=citation_context.citation_errors,
                eligible_claims=collect_claim_bundle(
                    state.completed_tool_calls.values(),
                    filter_invalid=True,
                ),
                completed_tool_calls=list(state.completed_tool_calls.values()),
            )
        )
        if not decision.passed:
            await self._emit(
                state,
                AgentEventType.STOP_GATE_EVALUATED,
                {
                    "blocked": True,
                    "reason_code": decision.reason_code.value,
                    "next_action": decision.next_action.value,
                    "available_evidence_tools": self._available_stop_gate_evidence_tools(state),
                    "pre_gate_marker_count": len(
                        _ANSWER_CITATION_MARKER_RE.findall(citation_context.publish_answer)
                    ),
                    "pre_gate_resolved_citation_count": len(citation_context.review_citations),
                    "citation_policy": citation_context.citation_policy.mode.value,
                },
            )
            await self._emit(
                state,
                AgentEventType.STOP_GATE_RELEASED,
                {
                    "reason_code": decision.reason_code.value,
                    "release_mode": "original_candidate",
                },
            )
            logger.info(
                "immediate stop gate blocked run {}: {} {} (release original answer)",
                state.run_id,
                decision.reason_code,
                decision.reason,
            )
        # 设计调整：stop gate 不再最终拦截。候选未通过时仍放行原始回答，
        # 由答案审查与发布链路决定最终输出，避免丢答案或替换成固定话术。
        return answer, result, citation_context, True

    def _available_stop_gate_evidence_tools(self, state: _RunState) -> list[str]:
        claim_tools = sorted(
            {
                str(claim["tool"])
                for claim in collect_claim_bundle(
                    state.completed_tool_calls.values(), filter_invalid=True
                )
                if claim.get("tool")
            }
        )
        if claim_tools:
            return claim_tools
        return sorted(set(self.profile.enabled_tools) & set(CLAIM_TOOL_NAMES))

    async def _fail_stop_gate(self, state: _RunState) -> None:
        message = "答案未通过引用完整性门控，已停止发布草稿。"
        state.status = AgentStatusType.FAILED
        state.message = message
        await self._emit(
            state,
            AgentEventType.RUN_FAILED,
            self._terminal_payload(state, {"message": message}),
        )

    def _build_stop_gate_continuation_prompt(self, question: str, instruction: str) -> str:
        return (
            f"{question}\n\n"
            "上一版回复被 ViKnow Immediate Stop Gate 判定为不可发布。\n"
            f"{instruction}\n"
            "请继续完成任务。不要复述 gate 说明；最终只输出可以发布给用户的答案。"
        )

    def _stop_gate_judge(self, op: AgentOp) -> StopGateJudge | None:
        if self.stop_gate_judge is not None:
            return self.stop_gate_judge
        if not self.profile.answer_review.gate_enabled:
            return None
        return LLMStopGateJudge(self._build_model(self._answer_review_model_config(op)))

    def _evaluate_stop_candidate(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
    ) -> tuple[StopGateDecision, _FinalAnswerCitationContext]:
        citation_context = self._final_answer_citation_context(
            op, state, answer, use_policy_classifier=False
        )
        if state.stop_gate is None:
            state.stop_gate = StopCandidateGate(judge=self._stop_gate_judge(op))
        decision = state.stop_gate.evaluate(
            StopGateInput(
                question=op.input or "",
                answer=answer,
                citation_policy=citation_context.citation_policy,
                resolved_citation_count=len(citation_context.review_citations),
                citation_errors=citation_context.citation_errors,
                eligible_claims=collect_claim_bundle(
                    state.completed_tool_calls.values(), filter_invalid=True
                ),
                completed_tool_calls=list(state.completed_tool_calls.values()),
                answer_plan=answer_plan_judge_context(
                    state.answer_plan,
                    state.evidence_ledger,
                    state.answer_plan_review,
                ),
            )
        )
        return decision, citation_context

    async def _try_publish_choice_fallback(
        self,
        op: AgentOp | None,
        state: _RunState,
        *,
        termination_reason: str,
        draft: str = "",
        error: str = "",
    ) -> bool:
        """One auditable low-confidence path; never restart the model or run budget."""
        if op is None or state.status != AgentStatusType.RUNNING:
            return False
        if termination_reason != "budget_exhausted" and not (
            self.profile.runtime_budget.enabled
            and self.profile.runtime_budget.capture_provisional_choice
        ):
            return False
        if (
            self.profile.answer_review.gate_enabled
            and not self.profile.answer_review.allow_low_confidence_output
        ):
            return False
        question = _run_original_question(state)
        selected_draft = next(
            (
                value
                for value in (
                    draft,
                    state.pending_final_candidate_text,
                    state.best_effort_answer,
                    state.streamed_text[state.choice_draft_start :],
                )
                if has_final_choice(question, value)
            ),
            "",
        )
        selection = state.provisional_choice
        if selection is not None and selection.original_question != question:
            selection = None
        handoff = (
            self.profile.runtime_budget.choice_evidence_handoff
            and self.profile.runtime_budget.capture_provisional_choice
        )
        choice_order = {}
        if handoff:
            override, streamed_option, choice_order = newer_stream_choice(state, question)
            if override:
                selection = None
                selected_draft = f"Final answer: {streamed_option}" if streamed_option else ""
            elif state.choice_decision_seen and selection is None:
                selected_draft = ""  # Withdrawal only invalidates older model decisions.
        fallback, fallback_reason = terminal_choice_answer(
            question,
            selected_draft,
            selection=selection,
            termination_reason=termination_reason,
            prefer_selection=handoff,
        )
        if not fallback:
            return False
        state.repair_diagnostics.append(
            {
                "action": "budget_fallback"
                if termination_reason == "budget_exhausted"
                else "terminal_choice_fallback",
                "termination_reason": termination_reason,
                "reason": fallback_reason,
                "error": error,
                "verified": False,
                **(
                    {
                        "choice_evidence": choice_evidence_status(state, question),
                        "choice_order": choice_order,
                    }
                    if handoff
                    else {}
                ),
                "candidate_claim_refs": selection.supporting_claim_refs if selection else [],
                "published_verified_claim_refs": [],
            }
        )
        context = _FinalAnswerCitationContext(
            raw_answer=fallback,
            resolved_answer=fallback,
            publish_answer=fallback,
            current_claims=[],
            claim_bundle=[],
            review_citations=[],
            publish_citations={},
            citation_errors=[],
            citation_policy=AnswerReviewCitationPolicy(),
        )
        await self._publish_final_answer(
            op,
            state,
            fallback,
            {"termination_reason": termination_reason, "fallback_reason": fallback_reason},
            citation_context=context,
        )
        return state.status == AgentStatusType.COMPLETED

    async def _publish_final_answer(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
        result: Any,
        *,
        answer_review: AnswerReviewResult | None = None,
        citation_context: _FinalAnswerCitationContext | None = None,
    ) -> None:
        if state.status != AgentStatusType.RUNNING:
            return
        review_unavailable = (
            self.profile.answer_review.distinguish_execution_failures
            and answer_review is not None
            and answer_review.execution_status in {"unavailable", "invalid_input", "invalid_output"}
        )
        if answer_review is not None and not review_unavailable:
            unresolved = [
                gap
                for slot in _run_answer_slots(state)
                for gap in state.evidence_ledger.slot_evidence.get(slot["id"], {}).get(
                    "unresolved_gaps", []
                )
            ]
            corrected = render_supported_choice(
                parse_choice_options(_run_original_question(state) or op.input or ""),
                answer_review.selected_choice,
                _review_evidence_facts(state),
                unresolved,
            )
            if corrected is not None:
                answer = corrected
                citation_context = self._final_answer_citation_context(op, state, answer)
            elif unresolved or any(
                risk.repair_action == "revise_answer" for risk in answer_review.risks
            ):
                fallback = unverified_choice_answer(
                    _run_original_question(state) or op.input or "", answer
                )
                if fallback:
                    answer = fallback
                    citation_context = self._final_answer_citation_context(op, state, answer)
        if citation_context is None:
            citation_context = self._final_answer_citation_context(
                op, state, answer, use_policy_classifier=not review_unavailable
            )
        state.status = AgentStatusType.COMPLETED
        if answer and not state.published_final_answer:
            state.published_final_answer = True
            if state.streamed_text.endswith(answer):
                answer = ""
            elif answer.startswith(state.streamed_text):
                answer = answer.removeprefix(state.streamed_text)
            if answer:
                await self._emit(state, AgentEventType.MESSAGE_DELTA, {"text": answer})
        if citation_context is None:
            citation_context = self._final_answer_citation_context(
                op,
                state,
                answer,
            )
        answer = citation_context.publish_answer
        citations = citation_context.publish_citations
        payload: dict[str, Any] = {
            "text": answer,
            "raw_model_output": _serialize_raw_model_output(result),
            "raw_model_output_source": "deepagents",
            "raw_model_output_stage": "final_output",
        }
        if state.answer_plan_review is not None:
            assessment = state.answer_plan_review.assessment
            payload["answer_disposition"] = {
                "kind": (
                    "answered" if assessment.conclusion == "answer_supported" else "unanswerable"
                ),
                "reason": assessment.conclusion_reason,
            }
        if citations:
            payload["citations"] = citations
        token_usage = _aggregate_token_usages(state.model_token_usages.values())
        usage_sink = _MODEL_USAGE_SINK.get()
        if usage_sink is not None:
            sink_usage = usage_sink.aggregate()
            if sink_usage:
                token_usage = _aggregate_token_usages([token_usage, sink_usage])
        if not token_usage:
            token_usage = _extract_token_usage(result)
        if token_usage:
            payload["token_usage"] = token_usage
        if answer_review is not None:
            payload["answer_review"] = answer_review.model_dump(mode="json")
        await self._emit(
            state,
            AgentEventType.MESSAGE_COMPLETED,
            payload,
        )
        final_answer_event_sequence = state.sequence
        if state.repair_diagnostics:
            for callback in self.langchain_callbacks:
                update = getattr(callback, "update_viknow_trace_metadata", None)
                if callable(update):
                    update(
                        state.run_id,
                        {
                            "viknow_evidence_repair": state.repair_diagnostics,
                            "viknow_final_evidence": citations,
                        },
                    )
        synced_trace_count = self._sync_final_answer_traces(state, answer)
        await self._emit(
            state,
            AgentEventType.RUN_COMPLETED,
            self._terminal_payload(
                state,
                {"session_id": state.session_id, "thread_id": state.thread_id},
                final_answer_event_sequence=final_answer_event_sequence,
                synced_trace_count=synced_trace_count,
            ),
        )

    async def _fail_answer_review(self, state: _RunState) -> None:
        message = "答案未通过可靠性审查，已停止发布低可信结果。"
        state.status = AgentStatusType.FAILED
        state.message = message
        await self._emit(
            state,
            AgentEventType.RUN_FAILED,
            self._terminal_payload(state, {"message": message}),
        )

    def _sync_final_answer_traces(self, state: _RunState, answer: str) -> int:
        for callback in self.langchain_callbacks:
            update_output = getattr(callback, "update_viknow_trace_outputs", None)
            if not callable(update_output):
                continue
            try:
                return int(update_output(state.run_id, {"final_answer": answer}) or 0)
            except Exception:
                logger.opt(exception=True).warning(
                    "[langfuse] failed to synchronize final answers for run {}",
                    state.run_id,
                )
        return 0

    def _terminal_payload(
        self,
        state: _RunState,
        payload: dict[str, Any],
        *,
        final_answer_event_sequence: int | None = None,
        synced_trace_count: int = 0,
    ) -> dict[str, Any]:
        result = dict(payload)
        binding = (
            self.langfuse_binding_provider()
            if self.langfuse_binding_provider is not None
            else self.langfuse_binding
        )
        binding_fields = (
            {
                "service_id": binding.service_id,
                "environment": binding.environment,
                "source_id": binding.source_id,
                "project_id": binding.project_id,
            }
            if binding.configured
            else {}
        )
        if state.repair_diagnostics:
            result["efficiency_diagnostics"] = list(state.repair_diagnostics)
        trace_ids: list[str] = []
        if state.usage_recorder is not None:
            trace_id = state.usage_recorder.trace_id()
            if trace_id:
                trace_ids.append(trace_id)
        for callback in self.langchain_callbacks:
            resolve_trace_ids = getattr(callback, "resolved_langfuse_trace_ids", None)
            if not callable(resolve_trace_ids):
                continue
            try:
                for trace_id in resolve_trace_ids(state.run_id):
                    normalized_trace_id = str(trace_id or "").strip()
                    if normalized_trace_id and normalized_trace_id not in trace_ids:
                        trace_ids.append(normalized_trace_id)
            except Exception:
                logger.opt(exception=True).warning(
                    "[langfuse] failed to resolve trace ids for run {}",
                    state.run_id,
                )
        if not trace_ids:
            reason = "trace_id_unresolved" if self.langchain_callbacks else "langfuse_disabled"
            result["trace_links"] = [
                RunTraceLink.unavailable(
                    reason,
                    final_answer_event_sequence=final_answer_event_sequence,
                    **binding_fields,
                ).model_dump(mode="json")
            ]
            return result
        trace_links: list[dict[str, Any]] = []
        for trace_id in trace_ids:
            try:
                trace_links.append(
                    RunTraceLink.resolved(
                        trace_id=trace_id,
                        trace_name=state.langfuse_trace_name,
                        final_answer_event_sequence=final_answer_event_sequence,
                        root_output_synced=synced_trace_count == len(trace_ids),
                        reason=None if binding.configured else "project_identity_unresolved",
                        **binding_fields,
                    ).model_dump(mode="json")
                )
            except ValueError:
                trace_links.append(
                    RunTraceLink.unavailable(
                        "invalid_langfuse_trace_id",
                        final_answer_event_sequence=final_answer_event_sequence,
                        **binding_fields,
                    ).model_dump(mode="json")
                )
        result["trace_links"] = trace_links
        return result

    def _update_answer_review_trace_metadata(
        self,
        state: _RunState,
        answer_review: AnswerReviewResult,
        *,
        event_sequence: int,
    ) -> None:
        metadata = {
            "viknow_answer_review": {
                **_answer_review_trace_payload(answer_review),
                "event_sequence": event_sequence,
            }
        }
        state.answer_review_trace_metadata = metadata
        for callback in self.langchain_callbacks:
            update_trace = getattr(callback, "update_viknow_trace_metadata", None)
            if not callable(update_trace):
                continue
            update_trace(state.run_id, metadata)

    def _update_tool_internal_model_usage_trace_metadata(
        self,
        state: _RunState,
        payload: dict[str, Any],
    ) -> None:
        metadata = _record_tool_internal_model_usage(state, payload)
        if not metadata:
            return
        for callback in self.langchain_callbacks:
            update_trace = getattr(callback, "update_viknow_trace_metadata", None)
            if not callable(update_trace):
                continue
            update_trace(state.run_id, metadata)

    async def _emit_streamed_snapshots(self, state: _RunState) -> None:
        if state.streamed_reasoning.strip() and not state.completed_reasoning_emitted:
            await self._emit(
                state,
                AgentEventType.REASONING_COMPLETED,
                {"text": state.streamed_reasoning, "format": "reasoning_content"},
            )
        if state.streamed_text.strip() and not state.completed_message_emitted:
            await self._emit(
                state,
                AgentEventType.MESSAGE_COMPLETED,
                {"text": state.streamed_text},
            )

    def _review_request(
        self,
        op: AgentOp,
        state: _RunState,
        citation_context: _FinalAnswerCitationContext | str,
        *,
        attempt: int,
        previous_feedback: str | None,
    ) -> AnswerReviewRequest:
        if isinstance(citation_context, str):
            citation_context = self._final_answer_citation_context(op, state, citation_context)
        evidence, evidence_relations = _evidence_bundle_from_tool_calls(
            state.completed_tool_calls.values()
        )
        evidence.extend(_historical_claim_evidence(state.historical_claims))
        return AnswerReviewRequest(
            answer_slots=_run_answer_slots(state),
            evidence_facts=_review_evidence_facts(state),
            choice_options=parse_choice_options(_run_original_question(state) or op.input or ""),
            question=_run_original_question(state) or op.input or "",
            answer=citation_context.raw_answer,
            evidence=evidence,
            evidence_relations=evidence_relations,
            citations=citation_context.review_citations,
            citation_errors=citation_context.citation_errors,
            citation_policy=citation_context.citation_policy,
            answer_plan=state.answer_plan.prompt_summary() if state.answer_plan else None,
            attempt=attempt,
            previous_feedback=previous_feedback,
        )

    def _final_answer_citation_context(
        self,
        op: AgentOp,
        state: _RunState,
        answer: str,
        *,
        use_policy_classifier: bool = True,
    ) -> _FinalAnswerCitationContext:
        current_claims = collect_claim_bundle(state.completed_tool_calls.values())
        claim_bundle = [
            *current_claims,
            *_aliased_historical_claim_bundle(state),
        ]
        resolve_result = resolve_citations(answer, claim_bundle)
        publish_answer, publish_citations = _final_answer_with_citations(
            answer,
            current_claims=current_claims,
            historical_claims=state.historical_claims,
            historical_claim_alias_refs=state.historical_claim_alias_refs,
            historical_message_citation_map=state.historical_message_citation_map,
            current_run_id=state.run_id,
            current_resolve_result=resolve_result,
        )
        review_citations = _answer_review_citations(
            resolve_result.citations,
            claim_bundle,
            current_run_id=state.run_id,
        )
        review_citations = _add_published_historical_citations(
            review_citations,
            publish_citations,
            claim_bundle,
        )
        citation_errors = [
            error
            for error in resolve_result.errors
            if not error.startswith("missing_citation:")
            and _citation_error_marker(error) not in publish_citations
        ]
        citation_policy_classifier = None
        if use_policy_classifier and (
            self.answer_review_policy_classifier is not None or self._answer_review_enabled()
        ):
            citation_policy_classifier = self._answer_review_policy_classifier(op)
        citation_policy = determine_citation_policy(
            question=op.input or "",
            answer=answer,
            completed_tool_calls=state.completed_tool_calls.values(),
            claim_bundle=claim_bundle,
            resolved_citation_count=len(resolve_result.citations),
            policy_classifier=citation_policy_classifier,
        )
        return _FinalAnswerCitationContext(
            raw_answer=answer,
            resolved_answer=resolve_result.resolved_text,
            publish_answer=publish_answer,
            current_claims=current_claims,
            claim_bundle=claim_bundle,
            review_citations=review_citations,
            publish_citations=publish_citations,
            citation_errors=citation_errors,
            citation_policy=citation_policy,
        )

    def _with_answer_review_citation_policy(
        self,
        op: AgentOp,
        state: _RunState,
        citation_context: _FinalAnswerCitationContext,
    ) -> _FinalAnswerCitationContext:
        citation_policy = determine_citation_policy(
            question=op.input or "",
            answer=citation_context.raw_answer,
            completed_tool_calls=state.completed_tool_calls.values(),
            claim_bundle=citation_context.claim_bundle,
            resolved_citation_count=len(citation_context.review_citations),
            policy_classifier=self._answer_review_policy_classifier(op),
        )
        return replace(citation_context, citation_policy=citation_policy)

    def _answer_reviewer(self, op: AgentOp) -> AnswerReviewer:
        return self.answer_reviewer or RuleBasedAnswerReviewer(
            pass_threshold=self.profile.answer_review.pass_threshold,
            claim_reviewer=self._answer_review_claim_reviewer(op),
            semantic_reviewer=self._answer_review_semantic_reviewer(op),
            claim_relevance_scorer=self.answer_review_claim_relevance_scorer,
        )

    def _answer_review_replay_input(
        self,
        op: AgentOp,
        request: AnswerReviewRequest,
    ) -> AnswerReviewReplayInput:
        return AnswerReviewReplayInput(
            request=request,
            config=self.profile.answer_review,
            reviewer_model=self._answer_review_model_config(op).resolve_model(),
        )

    def _answer_review_policy_classifier(
        self,
        op: AgentOp,
    ) -> CitationPolicyClassifier | None:
        if not self.profile.answer_review.llm_reviewers_enabled:
            return None
        if self.answer_review_policy_classifier is not None:
            return self.answer_review_policy_classifier
        return _LazyCitationPolicyClassifier(
            lambda: LLMPolicyClassifier(self._build_model(self._answer_review_model_config(op)))
        )

    def _answer_review_claim_reviewer(
        self,
        op: AgentOp,
    ) -> AnswerReviewClaimReviewer | None:
        if not self.profile.answer_review.llm_reviewers_enabled:
            return None
        if self.answer_review_claim_reviewer is not None:
            return self.answer_review_claim_reviewer
        return _LazyClaimReviewer(
            lambda: LLMClaimReviewer(self._build_model(self._answer_review_model_config(op)))
        )

    def _answer_review_semantic_reviewer(
        self,
        op: AgentOp,
    ) -> AnswerReviewSemanticReviewer | None:
        if not self.profile.answer_review.llm_reviewers_enabled:
            return None
        if self.answer_review_semantic_reviewer is not None:
            return self.answer_review_semantic_reviewer
        return _LazySemanticReviewer(
            lambda: LLMSemanticReviewer(self._build_model(self._answer_review_model_config(op)))
        )

    def _answer_review_enabled(self) -> bool:
        return self.profile.answer_review.enabled

    def _answer_review_model_config(self, op: AgentOp) -> AgentModelConfig:
        if self.answer_review_model_config is None:
            model_config = self._model_config_for_op(op)
        else:
            model_config = _agent_model_config_from_tool_model(self.answer_review_model_config)
        return model_config.model_copy(
            update={"timeout_seconds": self.profile.answer_review.timeout_seconds}
        )

    def _build_retry_prompt(
        self,
        question: str,
        answer: str,
        review: AnswerReviewResult,
        *,
        evidence_context: str = "",
    ) -> str:
        evidence_block = (
            f"\n\n本轮已经自动补充了以下视觉计数证据，请在最终答案中引用相应 token：\n"
            f"{evidence_context}"
            if evidence_context.strip()
            else ""
        )
        return (
            f"{question}\n\n"
            "上一版答案未通过 ViKnow 答案可靠性审查。\n"
            f"上一版答案：\n{answer}\n\n"
            f"审查反馈：{review.retry_instruction or review.summary}\n"
            "请重新进入 ViKnow 知识问答流程。如果本轮证据不足，"
            "继续使用已启用的知识检索或视频检查工具获取证据；"
            "不要只说明将要审查或将要检索。最终答案只能基于已获得的证据，"
            "删除没有证据支撑的结论。"
            f"{evidence_block}"
        )

    async def _maybe_auto_visual_ground_counting_evidence(
        self,
        op: AgentOp,
        state: _RunState,
    ) -> bool:
        if state.auto_visual_grounding_done:
            return False
        if "visual_ground" not in self.profile.enabled_tools:
            return False
        if not _is_static_visible_count_question(op.input or ""):
            return False
        if any(record.name == "visual_ground" for record in state.completed_tool_calls.values()):
            return False
        args = _auto_visual_ground_args(op.input or "", state.completed_tool_calls.values())
        if args is None:
            return False
        tool = _tool_by_name(self.tools, "visual_ground")
        if tool is None:
            return False

        state.auto_visual_grounding_done = True
        state.visual_ground_call_count += 1
        tool_call_id = f"auto_visual_ground_{len(state.completed_tool_calls) + 1}"
        arguments = json.dumps(args, ensure_ascii=False)
        await self._record_mapped_event(
            state,
            AgentEventType.TOOL_CALL_STARTED,
            {
                "id": tool_call_id,
                "name": "visual_ground",
                "arguments": arguments,
            },
        )
        try:
            result = await _ainvoke_tool_for_auto_grounding(tool, args)
        except Exception as exc:
            await self._record_mapped_event(
                state,
                AgentEventType.TOOL_CALL_FAILED,
                {
                    "id": tool_call_id,
                    "name": "visual_ground",
                    "arguments": arguments,
                    "error": str(exc),
                },
            )
            logger.warning("auto visual_ground failed: {}", exc)
            return False
        await self._record_mapped_event(
            state,
            AgentEventType.TOOL_CALL_COMPLETED,
            {
                "id": tool_call_id,
                "name": "visual_ground",
                "arguments": arguments,
                "result": result,
            },
        )
        return True

    async def _run_deepagent(
        self,
        op: AgentOp,
        state: _RunState,
        *,
        precompiled_knowledge_context: str = "",
    ) -> Any:
        usage_sink = _RunModelUsageSink()
        usage_sink_token = _MODEL_USAGE_SINK.set(usage_sink)
        recorder = self._generation_recorder_for(state)
        recorder_token = set_active_generation_recorder(recorder)
        register_trace_metadata_source(
            state.run_id,
            lambda: _build_trace_tool_metadata(op.input or "", state),
        )
        try:
            async with start_span("agent.loop", **{"agents.runtime.phase": "agent.loop"}):
                return await self._run_deepagent_with_usage_sink(
                    op,
                    state,
                    precompiled_knowledge_context=precompiled_knowledge_context,
                )
        finally:
            self._finalize_run_usage_metadata(op, state, recorder)
            unregister_trace_metadata_source(state.run_id)
            reset_active_generation_recorder(recorder_token)
            _MODEL_USAGE_SINK.reset(usage_sink_token)
            # 本轮 stop-gate 反馈已在工具结果中固化，清理暂存 payload。
            state.stop_gate_pending_payloads.clear()

    def _generation_recorder_for(self, state: _RunState) -> GenerationRecorder:
        if state.usage_recorder is not None:
            return state.usage_recorder
        handler = next(
            (
                callback
                for callback in self.langchain_callbacks
                if hasattr(callback, "record_generation")
            ),
            None,
        )
        state.usage_recorder = GenerationRecorder(
            viknow_run_id=state.run_id,
            handler=handler,
        )
        return state.usage_recorder

    def _finalize_run_usage_metadata(
        self,
        op: AgentOp,
        state: _RunState,
        recorder: GenerationRecorder,
    ) -> None:
        # The breakdown mirrors what the Langfuse trace exposes as generations:
        # the main agent chain (LangChain callback, tracked via model_token_usages)
        # plus the explicit structured-LLM generations accumulated in the recorder.
        # The usage sink is intentionally not merged here: it overlaps with both
        # sources (it captures main/review stream usage), which would inflate totals.
        main_usage = _aggregate_token_usages(state.model_token_usages.values())
        main_model = self._main_agent_model_name(op)
        if main_usage:
            recorder.add_usage(main_model, main_usage)
        breakdown = recorder.breakdown()
        if breakdown:
            self._write_run_usage_trace_metadata(state, breakdown)
        from agents.runtime.core.usage.pending_store import (
            empty_usage_snapshot,
            is_usage_push_active,
            register_pending_usage_push,
        )

        if not is_usage_push_active():
            return
        trace_id = recorder.trace_id()
        if not trace_id:
            logger.debug(
                "[usage] skip pending registration for run {}: trace id unresolved",
                state.run_id,
            )
            return
        try:
            register_pending_usage_push(
                kind="query",
                source_id=state.run_id,
                langfuse_trace_id=trace_id,
                snapshot=breakdown or empty_usage_snapshot(),
            )
        except Exception:
            logger.opt(exception=True).warning(
                "[usage] failed to register pending push for run {}",
                state.run_id,
            )

    def _write_run_usage_trace_metadata(
        self,
        state: _RunState,
        breakdown: dict[str, Any],
    ) -> None:
        metadata = {"viknow_usage_breakdown": breakdown, "usage_finalized": True}
        for callback in self.langchain_callbacks:
            update_trace = getattr(callback, "update_viknow_trace_metadata", None)
            if not callable(update_trace):
                continue
            update_trace(state.run_id, metadata)

    def _main_agent_model_name(self, op: AgentOp) -> str | None:
        try:
            model_config = self._model_config_for_op(op)
            return model_config.resolve_model() or model_config.require_model()
        except Exception:
            return None

    def _qa_plan_middleware(self, state: _RunState, original_question: str) -> QaPlanMiddleware:
        def projected_state(snapshot: QaPlanSnapshot) -> _RunState:
            return replace(
                state,
                answer_plan=snapshot.plan.model_copy(deep=True),
                answer_plan_review=snapshot.review.model_copy(deep=True)
                if snapshot.review
                else None,
            )

        def execute(snapshot: QaPlanSnapshot, args: dict[str, Any]) -> tuple[QaPlanSnapshot, str]:
            # Domain validation operates on a detached copy. Only the native
            # tools node's Command commits it; _RunState is not a second owner.
            scratch = projected_state(snapshot)
            content = self._answer_plan(scratch, args)
            return snapshot.model_copy(
                update={
                    "plan": scratch.answer_plan,
                    "review": scratch.answer_plan_review,
                }
            ), content

        def project(snapshot: QaPlanSnapshot) -> None:
            previous = {slot["id"]: slot for slot in _run_answer_slots(state)}
            current = {
                slot["id"]: slot
                for slot in _run_answer_slots(replace(state, answer_plan=snapshot.plan))
            }
            changed_task = _run_original_question(state) != snapshot.plan.original_question
            for slot_id in list(state.evidence_ledger.slot_evidence):
                if changed_task or previous.get(slot_id) != current.get(slot_id):
                    state.evidence_ledger.slot_evidence.pop(slot_id)
            state.answer_plan = snapshot.plan
            state.answer_plan_review = snapshot.review

        def observe(
            snapshot: QaPlanSnapshot, observations: list[QaPlanObservation]
        ) -> QaPlanSnapshot:
            for observation in observations:
                payload = parse_tool_payload(_serialize(observation.content))
                if observation.tool_name in {"list_doc_segments", "list_video_segments"}:
                    snapshot.plan = snapshot.plan.observe_catalog_result(payload)
                elif observation.tool_name == "knowledge_search":
                    snapshot.plan = snapshot.plan.observe_search_result(
                        query=str(observation.args.get("query") or ""),
                        library_id=str(observation.args.get("library_id") or ""),
                        payload=payload,
                    )
            return snapshot

        return QaPlanMiddleware(
            run_id=state.run_id,
            original_question=original_question,
            execute=execute,
            project=project,
            view=lambda snapshot: self._answer_plan_view(projected_state(snapshot)),
            observe=observe,
        )

    def _answer_plan(self, state: _RunState, args: dict[str, Any]) -> str:
        if state.answer_plan is None:
            return ToolResult.fail(
                "answer_plan",
                ToolErrorCode.INVALID_ARGUMENTS,
                "No run-scoped QA answer plan is active.",
            ).to_json()
        previous_plan = state.answer_plan
        try:
            command = AnswerPlanCommand.model_validate(args)
            if command.update is not None:
                result = self._update_answer_plan(state, command.update.model_dump(mode="json"))
                if _tool_result_has_error(result):
                    return result
            if command.review is not None:
                observation = _ANSWER_PLAN_OBSERVATION.get()
                if observation is None or observation.run_id != state.run_id:
                    raise ValueError(
                        "Review has no originating model observation; "
                        "use answer_plan view and reassess in a new model turn."
                    )
                if (
                    observation.revision != state.answer_plan.revision
                    or observation.evidence_digest != evidence_digest(state.evidence_ledger)
                ):
                    raise ValueError(
                        "Plan or evidence changed after this model observation; "
                        "reassess the current view in a new model turn."
                    )
                review = AnswerPlanReview.model_validate(command.review.model_dump())
                review.validate_against(
                    state.answer_plan,
                    state.evidence_ledger,
                    historical_claim_refs=frozenset(
                        claim.claim_ref for claim in state.historical_claims
                    ),
                )
                state.answer_plan_review = RecordedAnswerPlanReview(
                    assessment=review,
                    revision=observation.revision,
                    evidence_digest=observation.evidence_digest,
                )
        except (TypeError, ValueError) as exc:
            return answer_plan_error_result(
                exc, current_plan=self._answer_plan_view(state)
            ).to_json()
        view = self._answer_plan_view(state)
        if command.update is not None:
            changes = answer_plan_goal_changes(previous_plan, state.answer_plan)
            if changes:
                view["goal_changes"] = changes
        return ToolResult.ok("answer_plan", view).to_json()

    def _answer_plan_view(self, state: _RunState) -> dict[str, Any]:
        view = answer_plan_view(
            state.answer_plan,
            state.evidence_ledger,
            state.answer_plan_review,
            citations={
                _current_claim_ref(state.run_id, item): item["agent_citation"]
                for item in collect_claim_bundle(state.completed_tool_calls.values())
                if item.get("agent_citation")
            },
        )
        # Session-verified evidence retains its original run/call identity. It
        # is reusable by the agent, but is never inserted into this run's ledger.
        view["historical_evidence"] = [
            {
                "claim_ref": claim.claim_ref,
                "run_id": claim.run_id,
                "tool_call_id": claim.tool_call_id,
                "tool_name": claim.tool,
                "claim": claim.claim,
                "source": claim.source,
                "citation": f"[{claim.claim_ref}]",
            }
            for claim in state.historical_claims
        ]
        return view

    def _update_answer_plan(self, state: _RunState, args: dict[str, Any]) -> str:
        if state.answer_plan is None:
            return json.dumps(
                {
                    "tool": "answer_plan",
                    "error": {
                        "code": "answer_plan_unavailable",
                        "message": "No run-scoped QA answer plan is active.",
                    },
                },
                ensure_ascii=False,
            )
        try:
            update = AnswerPlanUpdate.model_validate(args)
            update = _canonicalize_answer_plan_claim_refs(state, update)
            requested_claim_refs = {
                ref
                for slot in update.slot_updates
                for ref in [
                    *slot.inspect_claim_refs,
                    *(ref for item in slot.verified_items for ref in item.inspect_claim_refs),
                ]
            }
            rejected_claims = [
                claim.claim_ref
                for claim in state.evidence_ledger.claims
                if claim.claim_ref in requested_claim_refs
                and claim.status != EvidenceClaimStatus.VERIFIED
            ]
            if rejected_claims:
                raise ValueError(
                    "inspect claim ref(s) exist but are not verified: "
                    + ", ".join(sorted(set(rejected_claims)))
                    + ". These references are not missing IDs. Check the original inspect "
                    "verification result; use other verified evidence, investigate further, "
                    "or leave the slot unresolved. Renaming a reference cannot verify it."
                )
            available_claim_refs = {
                claim.claim_ref
                for claim in state.evidence_ledger.claims
                if claim.status == EvidenceClaimStatus.VERIFIED
            } | {claim.claim_ref for claim in state.historical_claims}
            claim_segment_ids = {
                claim.claim_ref: _evidence_locator_catalog_segment_ids(
                    claim.locator,
                    state.answer_plan,
                )
                for claim in state.evidence_ledger.claims
                if claim.status == EvidenceClaimStatus.VERIFIED
            }
            claim_segment_ids.update(
                {
                    claim.claim_ref: _evidence_locator_catalog_segment_ids(
                        claim_locator({"source": claim.source, "tool": claim.tool}),
                        state.answer_plan,
                    )
                    for claim in state.historical_claims
                }
            )
            state.answer_plan = state.answer_plan.apply_update(
                update,
                available_claim_refs=available_claim_refs,
                claim_segment_ids=claim_segment_ids,
            )
        except (TypeError, ValueError) as exc:
            return ToolResult.fail(
                "answer_plan",
                ToolErrorCode.INVALID_ARGUMENTS,
                str(exc),
                detail="answer_plan_update_invalid",
                data=self._answer_plan_view(state),
            ).to_json()
        return json.dumps(
            {
                "tool": "answer_plan",
                "success": True,
                "answer_plan": state.answer_plan.prompt_summary(),
            },
            ensure_ascii=False,
        )

    async def _run_deepagent_with_usage_sink(
        self,
        op: AgentOp,
        state: _RunState,
        *,
        precompiled_knowledge_context: str = "",
    ) -> Any:
        state.recursion_step_offset = state.recursion_step
        state.recursion_step_base = None
        preference_block, memory_context_block = await self._load_run_memory_blocks(op)
        selected_assets_context = _selected_knowledge_assets_context(op.selected_knowledge_assets)
        library_scope_context = _library_scope_context(op.library_ids)
        state.historical_claims = self._load_historical_claims(op, state)
        session_events = self._session_events(op) or []
        state.session_evidence_ledger = build_run_evidence_ledger(
            event for event in session_events if event.run_id != state.run_id
        )
        state.historical_message_citation_map = self._load_historical_message_citation_map(
            op, state
        )
        run_middleware: list[Any] = [_ToolGateRecoveryMiddleware(state)]
        if (
            "knowledge-qa-v2" in self.profile.enabled_skills
            and "answer_plan" in self.profile.enabled_tools
        ):
            run_middleware.append(self._qa_plan_middleware(state, op.input or ""))
            run_middleware.append(
                _QaSearchRecoveryMiddleware(original_query=op.input or "", state=state)
            )
        if self.profile.oracle_collection:
            oracle_skill = next(
                (skill for skill in self.skills if skill.name == "evidence-oracle-collection"),
                None,
            )
            skill_path_root = (
                _BUBBLEWRAP_SKILL_SOURCE
                if self.config.workspace.virtual_mode
                else _FILESYSTEM_SKILL_SOURCE
            )
            skill_path = (
                f"{skill_path_root}/{oracle_skill.name}/SKILL.md"
                if oracle_skill is not None
                else ""
            )
            run_middleware.append(
                _EvidenceResearchMiddleware(
                    state,
                    required_skill_name=oracle_skill.name if oracle_skill is not None else "",
                    required_skill_path=skill_path,
                )
            )
        if op.skill_flow_gold_trace:
            run_middleware.append(
                _GoldToolSimulationMiddleware(
                    GoldToolSimulator(
                        SkillEvalCase(
                            case_id=state.run_id,
                            question=op.input or "",
                            metadata={"gold_trace": op.skill_flow_gold_trace},
                        )
                    )
                )
            )
        if self._stop_gate_active():
            stop_gate_context: _FinalAnswerCitationContext | None = None

            def available_stop_gate_evidence_tools() -> list[str]:
                return self._available_stop_gate_evidence_tools(state)

            def evaluate_stop_candidate(answer: str) -> StopGateDecision:
                nonlocal stop_gate_context
                decision, stop_gate_context = self._evaluate_stop_candidate(op, state, answer)
                return decision

            async def emit_stop_gate_evaluation(decision: StopGateDecision) -> None:
                if stop_gate_context is None:
                    raise RuntimeError("stop gate evaluation did not produce citation context")
                was_blocked = state.stop_gate_check_count > 0
                if not decision.passed:
                    state.stop_gate_middleware_observed = True
                    state.stop_gate_check_count += 1
                if decision.passed and not was_blocked:
                    return
                await self._emit(
                    state,
                    AgentEventType.STOP_GATE_EVALUATED,
                    {
                        "blocked": not decision.passed,
                        "reason_code": decision.reason_code.value,
                        "next_action": decision.next_action.value,
                        "available_evidence_tools": available_stop_gate_evidence_tools(),
                        "pre_gate_marker_count": len(
                            _ANSWER_CITATION_MARKER_RE.findall(stop_gate_context.publish_answer)
                        ),
                        "pre_gate_resolved_citation_count": len(stop_gate_context.review_citations),
                        "citation_policy": stop_gate_context.citation_policy.mode.value,
                    },
                )

            async def emit_stop_gate_release(decision: StopGateDecision) -> None:
                await self._emit(
                    state,
                    AgentEventType.STOP_GATE_RELEASED,
                    {
                        "reason_code": decision.reason_code.value,
                        "release_mode": "after_terminal_check",
                    },
                )

            def available_stop_gate_tokens() -> list[str]:
                return [
                    str(claim["agent_citation"])
                    for claim in collect_claim_bundle(
                        state.completed_tool_calls.values(), filter_invalid=True
                    )
                    if claim.get("agent_citation")
                ]

            run_middleware.append(
                _StopGateMiddleware(
                    state=state,
                    evaluate=evaluate_stop_candidate,
                    on_evaluated=emit_stop_gate_evaluation,
                    on_released=emit_stop_gate_release,
                    available_tokens=available_stop_gate_tokens,
                    evidence_tools=available_stop_gate_evidence_tools,
                )
            )
        run_middleware.extend(
            [
                _RunToolResultProjectionMiddleware(state),
                _RepeatedWorkspaceToolCallMiddleware(state),
                _VisualGroundLimitMiddleware(state),
            ]
        )
        run_context_block = _compose_run_context_block(
            precompiled_knowledge_context,
            selected_assets_context,
            library_scope_context,
            memory_context_block,
        )
        if run_context_block:
            run_middleware.append(_UserMemoryContextMiddleware(run_context_block))
        run_middleware.append(
            _HistoricalCitationCanonicalizationMiddleware(
                state,
                state.historical_message_citation_map,
            )
        )
        run_middleware.append(_RuntimeBudgetMiddleware(state))
        budget = self.profile.runtime_budget
        if budget.enabled and (
            budget.finalization_reserve_seconds
            or budget.finalization_reserve_steps
            or budget.capture_provisional_choice
        ):
            run_middleware.append(
                BudgetEfficiencyMiddleware(
                    state,
                    budget,
                    question=lambda: _run_original_question(state),
                    step_limit=lambda: self._run_recursion_step_limit(state),
                )
            )
        if budget.enabled and (
            budget.max_identical_attempts is not None or budget.no_progress_warning_after
        ):
            run_middleware.append(ToolEfficiencyMiddleware(state, budget))
        run_middleware.append(_CountingGroundingEnforcerMiddleware(op, state=state))
        uploads_prompt = await self._prepare_session_uploads(op)
        extra_tools: list[Any] = []
        if self._stop_gate_active():
            extra_tools.append(_stop_gate_check)
        if self.profile.oracle_collection:
            extra_tools.append(_evidence_oracle_gate_check)
        async with self._workspace_runtime(state.thread_id, op=op) as workspace:
            agent = await self._build_agent(
                workspace.backend,
                skill_source=workspace.skill_source,
                subagent_skill_sources=workspace.subagent_skill_sources,
                model_config=self._model_config_for_op(op),
                base_tools=self.tools,
                extra_tools=extra_tools or None,
                preference_block=preference_block,
                memory_enabled=op.memory_enabled,
                extra_middleware=run_middleware,
            )
            prompt = self._build_prompt(
                state.retry_instruction or _run_original_question(state) or op.input or "",
                runtime_context=uploads_prompt,
                skill_source=workspace.skill_source,
            )
            payload = {"messages": [{"role": "user", "content": prompt}]}
            recursion_limit = self.config.recursion_limit
            if self.profile.runtime_budget.enabled:
                recursion_limit = _runtime_graph_recursion_limit(self.profile.runtime_budget)
            runtime_config = {
                "configurable": {"thread_id": state.thread_id},
                "recursion_limit": recursion_limit,
                "metadata": self._trace_metadata(op, state),
            }
            if self.langchain_callbacks:
                runtime_config["callbacks"] = list(self.langchain_callbacks)
            stream_events = getattr(agent, "astream_events", None)
            loop_spans = SpanSet()
            try:
                if callable(stream_events):
                    result = None
                    async for event in _await_stream(
                        stream_events(
                            payload,
                            version=self.config.stream_events_version,
                            config=runtime_config,
                        )
                    ):
                        _observe_agent_loop_span(loop_spans, event, run_id=state.run_id)
                        if state.status != AgentStatusType.RUNNING or state.budget_exhausted:
                            break
                        langgraph_step = _event_langgraph_step(event)
                        if (
                            langgraph_step is not None
                            and not self.event_mapper.is_subagent_event(event)
                            and _observe_run_recursion_step(state, langgraph_step)
                        ):
                            await self._evaluate_runtime_budget(state)
                        if _is_final_chain_event(event):
                            data = event.get("data")
                            if isinstance(data, dict) and data.get("output") is not None:
                                result = data["output"]
                        if _should_record_model_token_usage(event):
                            token_usage = _extract_token_usage(event)
                        else:
                            token_usage = {}
                        if token_usage:
                            state.model_token_usages[str(event.get("run_id") or "__unknown__")] = (
                                token_usage
                            )
                        if event.get(
                            "event"
                        ) == "on_chat_model_end" and not _is_summarization_metadata(
                            event.get("metadata")
                        ):
                            data = event.get("data")
                            output = data.get("output") if isinstance(data, dict) else None
                            from agents.runtime.core.eval.recorder import active_model_call_trace_recorder

                            resolved_usage = _resolve_model_call_usage(
                                event=event,
                                state=state,
                                event_token_usage=token_usage,
                                output=output,
                            )
                            active_model_call_trace_recorder().on_model_end(
                                output,
                                resolved_usage,
                            )
                        mapped = self.event_mapper.map_deepagent_event(event)
                        if (
                            self.profile.runtime_budget.choice_evidence_handoff
                            and not self.event_mapper.is_subagent_event(event)
                            and not _is_summarization_metadata(event.get("metadata"))
                        ):
                            observe_choice_stream(
                                state,
                                event,
                                text=str(mapped[1].get("text") or "")
                                if mapped and mapped[0] == AgentEventType.MESSAGE_DELTA
                                else "",
                            )
                        if mapped is not None:
                            event_type, payload_data = mapped
                            await self._record_mapped_event(
                                state,
                                event_type,
                                payload_data,
                                trace_run_id=str(event.get("run_id") or ""),
                            )
                    if result is not None:
                        return result
                    drafted = _drafted_answer(state)
                    if drafted:
                        return drafted
                return await agent.ainvoke(payload, config=runtime_config)
            except Exception as exc:
                if type(exc).__name__ != "GraphRecursionError":
                    raise
                drafted = _drafted_answer(state)
                if not drafted:
                    raise
                logger.warning(
                    "run {} hit the graph recursion limit; publishing the drafted answer",
                    state.run_id,
                )
                return drafted
            finally:
                loop_spans.close()

    async def _record_mapped_event(
        self,
        state: _RunState,
        event_type: AgentEventType,
        payload: dict[str, Any],
        *,
        trace_run_id: str = "",
    ) -> None:
        if state.status != AgentStatusType.RUNNING:
            return
        if event_type == AgentEventType.MODEL_CALL_STARTED:
            await self._evaluate_runtime_budget(state)
        if event_type == AgentEventType.MESSAGE_DELTA:
            text = str(payload.get("text") or "")
            if not text.strip() and not state.pending_final_candidate_text.strip():
                return
            state.pending_final_candidate_text += text
            return
        if event_type == AgentEventType.REASONING_DELTA:
            state.streamed_reasoning += str(payload.get("text") or "")
        if event_type == AgentEventType.CONTEXT_COMPACTED:
            compaction_key = _context_compaction_key(payload)
            if compaction_key in state.context_compaction_keys:
                return
            state.context_compaction_keys.add(compaction_key)
        if event_type == AgentEventType.TOOL_CALL_STARTED:
            if str(payload.get("name") or "") != STOP_GATE_CHECK_TOOL_NAME:
                await self._flush_pending_candidate_text(state)
            call_id = str(payload.get("id") or "")
            if call_id in state.started_tool_call_ids:
                return
            state.started_tool_call_ids.add(call_id)
            _record_skill_read(state, payload)
        if event_type == AgentEventType.TOOL_CALL_COMPLETED:
            raw_call_id = str(payload.get("id") or "")
            call_id = _canonical_tool_call_event_id(state, trace_run_id, raw_call_id)
            payload = dict(payload)
            payload["id"] = call_id
            if (
                trace_run_id
                and trace_run_id != call_id
                and trace_run_id in state.started_tool_call_ids
            ):
                # Legacy UI/timeline bridge: stream_id points at the started card.
                payload["stream_id"] = trace_run_id
            if (
                raw_call_id in state.completed_tool_call_ids
                or call_id in state.completed_tool_call_ids
            ):
                return
            started_by_trace_id = (
                trace_run_id
                and call_id == trace_run_id
                and trace_run_id in state.started_tool_call_ids
            )
            if call_id not in state.started_tool_call_ids:
                state.started_tool_call_ids.add(call_id)
                if raw_call_id:
                    state.started_tool_call_ids.add(raw_call_id)
                if not started_by_trace_id:
                    await self._emit(
                        state,
                        AgentEventType.TOOL_CALL_STARTED,
                        _started_tool_payload(call_id, payload),
                    )
            state.completed_tool_call_ids.add(call_id)
            if raw_call_id:
                state.completed_tool_call_ids.add(raw_call_id)
            if trace_run_id:
                state.completed_tool_call_ids.add(trace_run_id)
            full_result = payload.get("debug_result")
            if full_result is not None:
                payload["result"] = full_result
            pending_payload = next(
                (
                    state.stop_gate_pending_payloads[key]
                    for key in (raw_call_id, call_id, trace_run_id)
                    if state.stop_gate_pending_payloads.get(key)
                ),
                None,
            )
            if pending_payload:
                # stop_gate_check 的完整反馈只存在于 pending payload，
                # 事件/trace 侧同样使用完整版本（工具参数只带最小触发字段）。
                payload["result"] = json.dumps(pending_payload, ensure_ascii=False)
            gate_payload = _tool_gate_payload(payload.get("result"))
            if gate_payload is not None:
                recovery = gate_payload.get("recovery")
                state.tool_gate_recoveries[str(payload.get("name") or "")] = {
                    "reason_code": str(gate_payload.get("reason_code") or ""),
                    "requirement_code": str(
                        recovery.get("requirement_code") if isinstance(recovery, dict) else ""
                    ),
                    "constraint": str(
                        recovery.get("constraint")
                        if isinstance(recovery, dict)
                        else gate_payload.get("next_action") or ""
                    ),
                    "available_values": list(
                        recovery.get("available_values", []) if isinstance(recovery, dict) else []
                    ),
                }
                await self._emit(
                    state,
                    AgentEventType.TOOL_GATE_EVALUATED,
                    {
                        "tool_name": str(payload.get("name") or ""),
                        "blocked": True,
                        "reason_code": str(gate_payload.get("reason_code") or ""),
                        "next_action": str(gate_payload.get("next_action") or ""),
                    },
                )
            else:
                state.tool_gate_recoveries.pop(str(payload.get("name") or ""), None)
            payload = _assign_completed_tool_payload_citations(state, payload)
            state.completed_tool_calls[call_id] = ToolCallRecord(
                id=call_id,
                name=str(payload.get("name") or ""),
                arguments=str(payload.get("arguments") or ""),
                result=str(payload.get("result") or ""),
            )
            _record_trace_tool_result(state, payload)
            self._update_tool_internal_model_usage_trace_metadata(state, payload)
        if event_type == AgentEventType.TOOL_CALL_FAILED:
            raw_call_id = str(payload.get("id") or "")
            call_id = _canonical_tool_call_event_id(state, trace_run_id, raw_call_id)
            payload = dict(payload)
            payload["id"] = call_id
            if (
                trace_run_id
                and trace_run_id != call_id
                and trace_run_id in state.started_tool_call_ids
            ):
                payload["stream_id"] = trace_run_id
            if raw_call_id in state.failed_tool_call_ids or call_id in state.failed_tool_call_ids:
                return
            started_by_trace_id = (
                trace_run_id
                and call_id == trace_run_id
                and trace_run_id in state.started_tool_call_ids
            )
            if call_id not in state.started_tool_call_ids:
                state.started_tool_call_ids.add(call_id)
                if raw_call_id:
                    state.started_tool_call_ids.add(raw_call_id)
                if not started_by_trace_id:
                    await self._emit(
                        state,
                        AgentEventType.TOOL_CALL_STARTED,
                        _started_tool_payload(call_id, payload),
                    )
            state.failed_tool_call_ids.add(call_id)
            if raw_call_id:
                state.failed_tool_call_ids.add(raw_call_id)
            if trace_run_id:
                state.failed_tool_call_ids.add(trace_run_id)
        await self._emit(state, event_type, payload)

    async def _flush_pending_candidate_text(self, state: _RunState) -> None:
        text = state.pending_final_candidate_text
        if not text:
            return
        state.pending_final_candidate_text = ""
        state.last_answer_candidate = text
        state.answer_candidates.append(text)
        del state.answer_candidates[:-8]
        if parse_choice_options(_run_original_question(state)):
            if has_final_choice(_run_original_question(state), text):
                state.best_effort_answer = text
            text, _ = hold_final_choice(text)
        if not text:
            return
        state.streamed_text += text
        await self._emit(state, AgentEventType.MESSAGE_DELTA, {"text": text})

    async def _build_agent(
        self,
        backend: Any,
        *,
        skill_source: str | None = None,
        model_config: AgentModelConfig | None = None,
        base_tools: Sequence[Any] | None = None,
        extra_tools: list[Any] | None = None,
        preference_block: str = "",
        memory_enabled: bool = False,
        extra_middleware: list[Any] | None = None,
        subagent_skill_sources: dict[str, str] | None = None,
    ) -> Any:
        native_qa_plan = any(isinstance(item, QaPlanMiddleware) for item in extra_middleware or [])
        factory = self._agent_factory or (
            create_qa_deep_agent if native_qa_plan else _load_create_deep_agent()
        )
        tools = list(base_tools if base_tools is not None else self.tools)
        if native_qa_plan:
            # The middleware supplies the only executable plan tool. Never
            # register the ResourceScheduler-wrapped registry entry alongside it.
            tools = [item for item in tools if getattr(item, "name", None) != "answer_plan"]
        if extra_tools:
            tools.extend(extra_tools)
        model = self._build_model(model_config)
        main_tools = tools
        mapped_subagents: list[dict[str, Any]] = []
        if self._delegation_active():
            from agents.runtime.core.subagents import filter_main_agent_tools

            mapped_subagents = _map_deepagents_subagents(
                self.subagent_specs,
                tools,
                skill_index=self._subagent_skills,
                skill_source=skill_source,
                subagent_skill_sources=subagent_skill_sources,
                middleware=_middleware_for_subagents(extra_middleware),
            )
            mapped_subagents.append(dict(_DISABLED_GENERAL_PURPOSE_SUBAGENT))
            main_tools = filter_main_agent_tools(tools, self.subagent_specs)
        kwargs: dict[str, Any] = {
            "model": model,
            "tools": main_tools,
            "backend": backend,
            "system_prompt": self._build_profile_system_prompt(
                preference_block,
                memory_enabled=memory_enabled,
            ),
        }
        if extra_middleware:
            kwargs["middleware"] = list(extra_middleware)
        if skill_source is not None:
            kwargs["skills"] = [skill_source]
        if mapped_subagents:
            kwargs["subagents"] = mapped_subagents
        checkpointer = await self._checkpointer()
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer
        create_summarization = _build_summarization_middleware_factory(self.config.summarization)
        with _patched_create_summarization_middleware(create_summarization):
            if mapped_subagents:
                _, task_description, task_system_prompt = _orchestrator_prompts(self.profile)
                with _patched_subagent_task_docs(
                    task_description=task_description,
                    system_prompt=task_system_prompt,
                ):
                    return factory(**kwargs)
            return factory(**kwargs)

    async def _checkpointer(self) -> Any | None:
        if self._checkpoints is None:
            return None
        return await self._checkpoints.get()

    def _build_model(self, model_config: AgentModelConfig | None = None) -> Any:
        model_config = self._resolve_model_provider(model_config or self.model_config)
        if self._model_factory is not None:
            return self._model_factory(model_config)
        if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
            raise ValueError(f"unsupported model provider: {model_config.provider}")

        kwargs: dict[str, Any] = {
            "model": model_config.require_model(),
            "base_url": model_config.resolve_base_url(),
            "api_key": model_config.require_api_key(),
            "temperature": model_config.temperature,
            "streaming": True,
            "stream_usage": True,
            "reasoning_content_roundtrip": model_config.reasoning_content_roundtrip,
        }
        kwargs.update(model_connection_options(model_config, cache=self._model_connections))
        timeout_seconds = _resolve_model_timeout_seconds(model_config, self.profile)
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        extra_body: dict[str, Any] = {}
        if model_config.enable_thinking is True:
            extra_body["chat_template_kwargs"] = {"enable_thinking": True}
        if model_config.repetition_penalty is not None:
            extra_body["repetition_penalty"] = model_config.repetition_penalty
        if (
            private_vllm_priority(
                Priority.INTERACTIVE,
                provider_name=model_config.provider_name,
                ref=model_config.ref,
                model=model_config.model,
            )
            is not None
        ):
            extra_body = extra_body_with_priority(extra_body, Priority.INTERACTIVE)
        if extra_body:
            kwargs["extra_body"] = extra_body
        if model_config.max_input_tokens is not None:
            kwargs["profile"] = {"max_input_tokens": model_config.max_input_tokens}
        usage_trace_file = os.getenv(_MODEL_USAGE_TRACE_FILE_ENV)
        return ScheduledViKnowChatOpenAI(
            scheduler=self._scheduler,
            resource_class=CHAT_LLM_RESOURCE if self._scheduler is not None else None,
            usage_trace_file=Path(usage_trace_file).expanduser() if usage_trace_file else None,
            **kwargs,
        )

    def _model_config_for_op(self, op: AgentOp) -> AgentModelConfig:
        agent_model = (op.agent_model or "").strip()
        if not agent_model:
            return self.model_config
        for opt in self.model_options:
            if opt.selection_id() != agent_model:
                continue
            if opt.ref:
                return self.model_config.model_copy(
                    update={
                        "ref": opt.ref,
                        "provider_name": None,
                        "model": None,
                        "model_env": None,
                        "max_input_tokens": (
                            opt.max_input_tokens
                            if opt.max_input_tokens is not None
                            else self.model_config.max_input_tokens
                        ),
                    }
                )
            return self._non_ref_model_option_config(opt, agent_model)
        provider_model = _split_provider_qualified_model(agent_model)
        if provider_model is not None:
            provider_name, model = provider_model
            if provider_name in self.model_providers:
                return self.model_config.model_copy(
                    update={
                        "ref": f"{provider_name}:{model}",
                        "provider_name": None,
                        "model": None,
                        "model_env": None,
                    }
                )
        return self.model_config.model_copy(update={"model": agent_model, "gateway": None})

    def _non_ref_model_option_config(
        self,
        option: AgentModelOption,
        selection_id: str,
    ) -> AgentModelConfig:
        provider_name = option.provider_name or self.model_config.provider_name
        if provider_name is None and self.model_config.ref:
            provider_model = _split_provider_qualified_model(self.model_config.ref)
            if provider_model is None:
                raise ValueError("agent.model.ref must use provider:model")
            provider_name, _model = provider_model

        provider_config = self.model_providers.get(provider_name) if provider_name else None
        if provider_name and provider_config is None:
            raise ValueError(f"unknown model provider: {provider_name}")
        provider = provider_config.provider if provider_config else self.model_config.provider
        base_url = provider_config.base_url if provider_config else self.model_config.base_url
        base_url_env = (
            provider_config.base_url_env if provider_config else self.model_config.base_url_env
        )
        api_key_env = (
            provider_config.api_key_env if provider_config else self.model_config.api_key_env
        )
        reasoning_content_roundtrip = (
            provider_config.reasoning_content_roundtrip
            if provider_config
            else self.model_config.reasoning_content_roundtrip
        )
        if option.base_url_env is not None:
            base_url = None
            base_url_env = option.base_url_env
        if option.api_key_env is not None:
            api_key_env = option.api_key_env

        model = option.model
        if model is None and option.model_env is None:
            model = selection_id
        return self.model_config.model_copy(
            update={
                "ref": None,
                "provider": provider,
                "provider_name": None,
                "model": model,
                "model_env": option.model_env,
                "gateway": None,
                "base_url": base_url,
                "base_url_env": base_url_env,
                "api_key_env": api_key_env,
                "reasoning_content_roundtrip": reasoning_content_roundtrip,
                "max_input_tokens": (
                    option.max_input_tokens
                    if option.max_input_tokens is not None
                    else self.model_config.max_input_tokens
                ),
            }
        )

    def _resolve_model_provider(self, model_config: AgentModelConfig) -> AgentModelConfig:
        return resolve_model_config(
            model_config,
            self.model_providers,
            field_name="agent.model",
        )

    def _trace_metadata(self, op: AgentOp, state: _RunState) -> dict[str, Any]:
        agent_model = (op.agent_model or "").strip() or self.model_config.resolve_model()
        tags = ["viknow", "deepagents"]
        if op.trace_capture != TraceCaptureMode.NONE:
            tags.append("eval")
        metadata: dict[str, Any] = {
            "langfuse_user_id": op.user_id,
            "langfuse_session_id": state.session_id,
            "langfuse_tags": tags,
            "viknow_run_id": state.run_id,
            "viknow_query": op.input or "",
            "viknow_session_id": state.session_id,
            "viknow_workspace_id": op.workspace_id,
            "viknow_thread_id": state.thread_id,
            "viknow_agent_kernel": "deepagents",
            "viknow_agent_profile": self.profile.id,
            # Keep the run-scoped knowledge allowlist directly visible on the
            # Langfuse root trace. ``None`` means unrestricted; an empty list
            # means that no knowledge library is allowed for this run.
            "library_ids": None if op.library_ids is None else list(op.library_ids),
        }
        if agent_model:
            metadata["viknow_agent_model"] = agent_model
        if op.langfuse_trace_id:
            metadata["langfuse_trace_id"] = op.langfuse_trace_id
            metadata["viknow_eval_trace_id"] = op.langfuse_trace_id
        if op.langfuse_trace_source_id:
            metadata["viknow_eval_trace_source_id"] = op.langfuse_trace_source_id
        metadata["langfuse_trace_name"] = op.langfuse_trace_name or _DEFAULT_LANGFUSE_TRACE_NAME
        return metadata

    @asynccontextmanager
    async def _workspace_runtime(
        self,
        thread_id: str,
        *,
        op: AgentOp | None = None,
    ) -> AsyncIterator[_WorkspaceRuntime]:
        workspace_dir = self.config.workspace.root_dir / _safe_thread_path(thread_id)
        # JuiceFS/FUSE 上的 mkdir/rmtree/copytree 是同步阻塞 I/O；必须离开事件循环，
        # 否则整个 uvicorn 进程在 agent run 准备阶段会表现为“localhost 卡死”。
        skills = self._skills_to_stage()
        subagent_skills = self._subagent_skills_to_stage()

        def _prepare_host_workspace() -> bool:
            workspace_dir.mkdir(parents=True, exist_ok=True)
            for subdir in _RESTORE_WORKSPACE_PREFIXES + _CAPTURE_WORKSPACE_PREFIXES:
                (workspace_dir / subdir).mkdir(parents=True, exist_ok=True)
            return _stage_runtime_skills(workspace_dir, skills, subagent_skills)

        has_runtime_skills = await asyncio.to_thread(_prepare_host_workspace)
        if self.config.workspace.backend == WorkspaceBackendType.FILESYSTEM:
            from deepagents.backends.filesystem import FilesystemBackend

            try:
                skill_source = (
                    _FILESYSTEM_SKILL_SOURCE
                    if has_runtime_skills and self.config.workspace.virtual_mode
                    else str(workspace_dir / _RUNTIME_SKILLS_DIR)
                    if has_runtime_skills
                    else None
                )
                backend = FilesystemBackend(
                    root_dir=workspace_dir,
                    virtual_mode=self.config.workspace.virtual_mode,
                    max_file_size_mb=self.config.workspace.max_file_size_mb,
                )
                with bind_workspace_artifacts(backend, root=""):
                    yield _WorkspaceRuntime(
                        backend=backend,
                        skill_source=skill_source,
                        subagent_skill_sources=_subagent_skill_source_paths(
                            skill_source,
                            subagent_skills,
                        ),
                    )
            finally:
                await self._finalize_filesystem_workspace(op, workspace_dir)
            return
        if self.config.workspace.backend == WorkspaceBackendType.BUBBLEWRAP:
            runtime = await asyncio.to_thread(self._create_bubblewrap_sandbox)
            sandbox_backend = runtime.backend
            setup_completed = False
            has_error = False
            try:
                await self._restore_bubblewrap_workspace(sandbox_backend, workspace_dir)
                await asyncio.to_thread(_prepare_bubblewrap_workspace_access, sandbox_backend)
                await asyncio.to_thread(self._probe_bubblewrap_runtime, sandbox_backend)
                setup_completed = True
                skill_source = _BUBBLEWRAP_SKILL_SOURCE if has_runtime_skills else None
                backend = _with_artifacts_root(sandbox_backend, _WORKSPACE_ROOT)
                with bind_workspace_artifacts(backend, root=_WORKSPACE_ROOT):
                    yield _WorkspaceRuntime(
                        backend=backend,
                        skill_source=skill_source,
                        subagent_skill_sources=_subagent_skill_source_paths(
                            skill_source,
                            subagent_skills,
                        ),
                    )
            except BaseException:
                has_error = True
                raise
            finally:
                try:
                    if setup_completed:
                        await self._capture_bubblewrap_workspace(
                            sandbox_backend,
                            workspace_dir,
                            op=op,
                        )
                except Exception:
                    if not has_error:
                        raise
                finally:
                    if runtime.close is not None:
                        runtime.close()
            return
        raise ValueError(f"unsupported workspace backend: {self.config.workspace.backend}")

    async def _finalize_filesystem_workspace(
        self,
        op: AgentOp | None,
        workspace_dir: Path,
    ) -> None:
        def _capture() -> list[tuple[str, bytes]]:
            captured: list[tuple[str, bytes]] = []
            for prefix in _CAPTURE_WORKSPACE_PREFIXES:
                root = workspace_dir / prefix
                if not root.exists():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    relative = path.relative_to(workspace_dir).as_posix()
                    captured.append((f"{_WORKSPACE_ROOT}/{relative}", path.read_bytes()))
            return captured

        captured = await asyncio.to_thread(_capture)
        await self._finalize_session_workspace(op, workspace_dir, captured)

    def _create_bubblewrap_sandbox(self) -> _WorkspaceRuntime:
        if platform.system() != "Linux":
            raise RuntimeError(
                "bubblewrap sandbox requires Linux, bubblewrap, and cgroup v2; "
                "run ViKnow in the devcontainer or on Ubuntu."
            )
        from deepagents_sandbox import Sandbox, SandboxConfig, detect

        failed_checks = [check for check in detect.check_all() if not check.passed]
        if failed_checks:
            details = "; ".join(check.reason for check in failed_checks)
            raise RuntimeError(
                "bubblewrap sandbox requires Linux, bubblewrap, user namespaces, "
                f"and cgroup v2: {details}"
            )

        config = SandboxConfig(
            memory_limit_mb=self.config.sandbox.memory_limit_mb,
            max_pids=self.config.sandbox.max_pids,
            cpu_shares=self.config.sandbox.cpu_shares,
            timeout_seconds=self.config.sandbox.timeout_seconds,
            max_output_bytes=self.config.sandbox.max_output_bytes,
            network_access=self.config.sandbox.network_access,
            gpu=self.config.sandbox.gpu,
            extra_bind_mounts=list(self.config.sandbox.extra_bind_mounts),
            extra_env=dict(
                self.config.sandbox.extra_env,
                **{
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                },
            ),
        )
        sandbox = Sandbox(config=config, default_timeout=int(self.config.sandbox.timeout_seconds))
        return _WorkspaceRuntime(backend=sandbox, close=getattr(sandbox, "close", None))

    def _user_message_attachments(self, op: AgentOp) -> list[dict[str, object]]:
        manager = self.filesystem_manager
        if manager is None or not op.session_id or not op.attachment_file_ids:
            return []
        return manager.user_message_attachments(
            user_id=op.user_id,
            session_id=op.session_id,
            file_ids=op.attachment_file_ids,
        )

    async def _prepare_session_uploads(self, op: AgentOp) -> str:
        manager = self.filesystem_manager
        if manager is None or not op.session_id or not op.workspace_id:
            return ""
        from agents.runtime.core.filesystem.manager import format_uploads_prompt

        entries = await asyncio.to_thread(
            manager.prepare_run_workspace,
            user_id=op.user_id,
            workspace_id=op.workspace_id,
            session_id=op.session_id,
            attachment_file_ids=op.attachment_file_ids,
        )
        return format_uploads_prompt(entries)

    async def _finalize_session_workspace(
        self,
        op: AgentOp | None,
        workspace_dir: Path,
        captured_files: list[tuple[str, bytes]],
    ) -> None:
        """把捕获到的沙箱文件交给 FilesystemManager 做会话级持久化收尾。

        将 /workspace/... 转为 staging 相对路径，再调用 finalize_run_workspace
        同步到 MinIO / session_files。bubblewrap capture 与 filesystem backend
        finalize 共用此出口。
        """
        manager = self.filesystem_manager
        if manager is None or op is None or not op.session_id or not op.workspace_id:
            return
        relative_files = [
            (str(relative), content)
            for sandbox_path, content in captured_files
            if (relative := _workspace_relative_path(sandbox_path)) is not None
        ]
        await asyncio.to_thread(
            manager.finalize_run_workspace,
            user_id=op.user_id,
            workspace_id=op.workspace_id,
            session_id=op.session_id,
            captured_files=relative_files,
        )

    async def _restore_bubblewrap_workspace(self, backend: Any, workspace_dir: Path) -> None:
        """Run 开始前把宿主机 staging 内容灌进 bubblewrap 沙箱。

        1. 从 staging 收集 uploads/ 与 .skills/ 下的文件
        2. 分批 upload_files 写入沙箱 /workspace/...
        3. _prepare_bubblewrap_workspace_access 修正沙箱内权限

        文件收集走线程池：workspace 可能在 JuiceFS（FUSE）上，同步 rglob/read_bytes
        会阻塞 asyncio 事件循环，表现为整个 5174 服务卡死。
        """
        uploads = await asyncio.to_thread(
            lambda: (
                _workspace_uploads(workspace_dir, prefixes=_RESTORE_WORKSPACE_PREFIXES)
                + _runtime_skill_uploads(workspace_dir)
            )
        )
        if not uploads:
            return
        for batch in _batch(uploads, 25):
            responses = await asyncio.to_thread(backend.upload_files, batch)
            errors = [response for response in responses if getattr(response, "error", None)]
            if errors:
                first = errors[0]
                raise RuntimeError(f"failed to restore workspace file {first.path}: {first.error}")
            await asyncio.to_thread(
                _prepare_bubblewrap_workspace_access,
                backend,
                [path for path, _content in batch],
            )

    async def _capture_bubblewrap_workspace(
        self,
        backend: Any,
        workspace_dir: Path,
        *,
        op: AgentOp | None = None,
    ) -> None:
        """Run 结束后从沙箱拉回产物到宿主机 staging。

        1. 在沙箱里列出 outputs/、scratch/ 下的路径
        2. 分批 download_files 读内容（过大文件跳过）
        3. _merge_host_workspace 写回 staging
        4. 删掉 staging 里临时的 .skills/
        5. _finalize_session_workspace 把产物同步进 Filesystem Manager / MinIO
        """
        paths = await asyncio.to_thread(
            _list_workspace_paths,
            backend,
            prefixes=_CAPTURE_WORKSPACE_PREFIXES,
        )
        files: list[tuple[str, bytes]] = []
        max_bytes = self.config.workspace.max_file_size_mb * 1024 * 1024
        for batch in _batch(paths, 25):
            responses = await asyncio.to_thread(backend.download_files, batch)
            for response in responses:
                if getattr(response, "error", None) or getattr(response, "content", None) is None:
                    continue
                raw_content = response.content
                content = (
                    raw_content
                    if isinstance(raw_content, bytes)
                    else str(raw_content).encode("utf-8")
                )
                if len(content) > max_bytes:
                    continue
                files.append((str(response.path), content))
        await asyncio.to_thread(
            _merge_host_workspace,
            workspace_dir,
            files,
            prefixes=_CAPTURE_WORKSPACE_PREFIXES,
        )
        skills_dir = workspace_dir / _RUNTIME_SKILLS_DIR

        def _cleanup_skills() -> None:
            if skills_dir.exists():
                shutil.rmtree(skills_dir)

        await asyncio.to_thread(_cleanup_skills)
        await self._finalize_session_workspace(op, workspace_dir, files)

    def _probe_bubblewrap_runtime(self, backend: Any) -> None:
        execute = getattr(backend, "execute", None)
        if not callable(execute):
            raise RuntimeError("bubblewrap sandbox backend does not support execute")
        result = execute(
            "printf ready > /workspace/.viknow-sandbox-probe "
            "&& test -w /workspace "
            "&& rm -f /workspace/.viknow-sandbox-probe "
            "&& echo ready",
            timeout=5,
        )
        exit_code = getattr(result, "exit_code", None)
        output = str(getattr(result, "output", "") or "")
        if exit_code not in (None, 0):
            raise RuntimeError(
                f"bubblewrap sandbox readiness probe failed (exit_code={exit_code}): "
                f"{output.strip()}"
            )
        if "ready" not in output:
            raise RuntimeError("bubblewrap sandbox readiness probe failed: missing ready output")

    async def _budget_watchdog(self, state: _RunState) -> None:
        budget = self.profile.runtime_budget
        if not budget.enabled:
            return
        while state.status == AgentStatusType.RUNNING and not state.budget_exhausted:
            await asyncio.sleep(0.5)
            await self._evaluate_runtime_budget(state)

    def _run_recursion_step_limit(self, state: _RunState) -> int:
        # Replanning can grant configured capacity, but cannot revoke capacity
        # already used by this run. Repeated revisions never add extra steps.
        state.granted_recursion_step_limit = max(
            state.granted_recursion_step_limit,
            _runtime_budget_recursion_step_limit(self.profile.runtime_budget, state.answer_plan),
        )
        return state.granted_recursion_step_limit

    async def _evaluate_runtime_budget(self, state: _RunState) -> None:
        budget = self.profile.runtime_budget
        if not budget.enabled or state.status != AgentStatusType.RUNNING:
            return
        elapsed = max(0.0, time.monotonic() - state.budget_started_at)
        max_recursion_steps = self._run_recursion_step_limit(state)
        warn_steps = max(1, math.ceil(max_recursion_steps * budget.warn_ratio))
        warn_seconds = budget.time_limit_seconds * budget.warn_ratio
        if not state.budget_finalizing and (
            (
                budget.finalization_reserve_seconds > 0
                and elapsed >= budget.time_limit_seconds - budget.finalization_reserve_seconds
            )
            or (
                budget.finalization_reserve_steps > 0
                and state.recursion_step >= max_recursion_steps - budget.finalization_reserve_steps
            )
        ):
            state.budget_finalizing = True
            state.repair_diagnostics.append(
                {
                    "action": "finalization_reserve",
                    "verified": False,
                    "remaining_seconds": max(0, budget.time_limit_seconds - elapsed),
                }
            )
        step_exhausted = state.recursion_step >= max_recursion_steps
        time_exhausted = elapsed >= budget.time_limit_seconds
        if step_exhausted or time_exhausted:
            await self._emit_budget_exhausted(
                state,
                elapsed=elapsed,
                dimensions=_budget_trigger_dimensions(
                    step_hit=step_exhausted,
                    time_hit=time_exhausted,
                ),
            )
            return
        step_warning = state.recursion_step >= warn_steps
        time_warning = elapsed >= warn_seconds
        if step_warning or time_warning:
            await self._emit_budget_warning(
                state,
                elapsed=elapsed,
                dimensions=_budget_trigger_dimensions(
                    step_hit=step_warning,
                    time_hit=time_warning,
                ),
            )

    async def _emit_budget_warning(
        self,
        state: _RunState,
        *,
        elapsed: float,
        dimensions: list[str],
    ) -> None:
        if state.budget_exhausted:
            return
        budget = self.profile.runtime_budget
        max_recursion_steps = self._run_recursion_step_limit(state)
        payload = _budget_event_payload(
            severity="warning",
            dimensions=dimensions,
            recursion_step=state.recursion_step,
            elapsed_seconds=elapsed,
            max_recursion_steps=max_recursion_steps,
            time_limit_seconds=budget.time_limit_seconds,
        )
        state.budget_warning_notice = _budget_tool_notice(payload)
        state.budget_prompt_block = _render_budget_prompt_block(
            severity="warning",
            payload=payload,
        )
        if state.budget_warning_emitted:
            return
        state.budget_warning_emitted = True
        await self._emit(state, AgentEventType.RUN_BUDGET_WARNING, payload)

    async def _emit_budget_exhausted(
        self,
        state: _RunState,
        *,
        elapsed: float,
        dimensions: list[str],
    ) -> None:
        if state.budget_exhausted:
            return
        budget = self.profile.runtime_budget
        max_recursion_steps = self._run_recursion_step_limit(state)
        state.budget_exhausted = True
        state.budget_warning_emitted = True
        state.budget_abort.set()
        if self._scheduler is not None:
            self._scheduler.cancel_source_units(state.run_id, dimensions=tuple(dimensions))
        payload = _budget_event_payload(
            severity="exhausted",
            dimensions=dimensions,
            recursion_step=state.recursion_step,
            elapsed_seconds=elapsed,
            max_recursion_steps=max_recursion_steps,
            time_limit_seconds=budget.time_limit_seconds,
        )
        state.budget_prompt_block = _render_budget_prompt_block(
            severity="exhausted",
            payload=payload,
        )
        message = (
            "runtime budget exhausted "
            f"(dimensions={','.join(dimensions)}, "
            f"elapsed_seconds={round(elapsed, 3)}, "
            f"recursion_step={state.recursion_step})"
        )
        record_error(ERROR_RUNTIME_BUDGET_EXHAUSTED, message)
        await self._emit(state, AgentEventType.RUN_BUDGET_EXHAUSTED, payload)
        if await self._try_publish_choice_fallback(
            state.original_op, state, termination_reason="budget_exhausted"
        ):
            current = asyncio.current_task()
            if state.task is not None and state.task is not current and not state.task.done():
                state.task.cancel()
            return
        state.status = AgentStatusType.FAILED
        state.message = message
        await self._emit(
            state,
            AgentEventType.RUN_FAILED,
            self._terminal_payload(state, {"message": message, **payload}),
        )
        current = asyncio.current_task()
        if state.task is not None and state.task is not current and not state.task.done():
            state.task.cancel()

    async def _emit(
        self,
        state: _RunState,
        event_type: AgentEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        state.sequence += 1
        event_payload = dict(payload or {})
        if event_type in _TERMINAL_EVENT_TYPES:
            event_payload["evidence_ledger"] = state.evidence_ledger.model_dump(mode="json")
        event = AgentEvent(
            type=event_type,
            run_id=state.run_id,
            sequence=state.sequence,
            payload=event_payload,
        )
        self.event_store.append(event)
        state.queue.put_nowait(event)
        state.evidence_ledger.apply(event)
        if event_type in {
            AgentEventType.MESSAGE_DELTA,
            AgentEventType.REASONING_DELTA,
            AgentEventType.MESSAGE_COMPLETED,
        }:
            from agents.runtime.core.eval.recorder import active_model_call_trace_recorder

            active_model_call_trace_recorder().on_stream_token()
        if event_type == AgentEventType.MESSAGE_COMPLETED:
            state.completed_message_emitted = True
        elif event_type == AgentEventType.REASONING_COMPLETED:
            state.completed_reasoning_emitted = True

    def _resolve_run(self, run_id: str | None) -> _RunState:
        if run_id is None:
            if len(self._runs) != 1:
                raise ValueError("run_id is required when runtime has multiple runs")
            return next(iter(self._runs.values()))
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return state

    def _build_prompt(
        self,
        question: str,
        *,
        runtime_context: str = "",
        skill_source: str | None = None,
    ) -> str:
        prompt = f"user_query:\n{self.profile.user_prompt_template.format(input=question)}"
        if runtime_context.strip():
            prompt = f"{prompt}\n\n## Runtime Context\n\n{runtime_context.strip()}"
        reminder = self._skill_reminder_text(skill_source=skill_source)
        if not reminder:
            return prompt
        return f"{prompt}\n\n## Skill Reminder\n\n{reminder}"

    def _skill_reminder_text(self, *, skill_source: str | None = None) -> str:
        if not self.config.skill_reminder.enabled or not self.skills:
            return ""
        if skill_source is None:
            raise RuntimeError("skill_source is required when skill reminders are enabled")
        return _render_skill_reminder(
            self.skills,
            skill_path_root=skill_source,
        )

    async def _load_precompiled_knowledge_context(self, op: AgentOp) -> str:
        loader = self._precompiled_knowledge_loader
        if loader is None:
            return ""
        query = str(op.input or "").strip()
        if not query:
            return ""
        library_id = str(op.workspace_id or "").strip()
        if not library_id:
            return ""
        if op.library_ids is not None and library_id not in op.library_ids:
            return ""
        async with start_span("precompile_kb", **{"agents.runtime.phase": "precompile_kb"}):
            try:
                return await loader(library_id=library_id, query=query)
            except Exception:
                return ""

    async def _load_run_memory_blocks(self, op: AgentOp) -> tuple[str, str]:
        if not op.memory_enabled:
            return "", ""
        query = op.input or ""
        preference_block = await self.user_memory_service.load_preference_context(
            user_id=op.user_id,
            query=query,
        )
        memory_context_block = await self.user_memory_service.load_memory_context(
            user_id=op.user_id,
            query=query,
        )
        return preference_block, memory_context_block

    def _load_historical_claims(self, op: AgentOp, state: _RunState) -> list[HistoricalClaim]:
        events = self._session_events(op)
        if events is None:
            return []
        return build_session_evidence_ledger(
            events,
            current_run_id=state.run_id,
            limit=20,
        )

    def _load_historical_message_citation_map(
        self, op: AgentOp, state: _RunState
    ) -> HistoricalMessageCitationMap:
        events = self._session_events(op)
        if events is None:
            return HistoricalMessageCitationMap()
        return build_session_message_citation_map(events, current_run_id=state.run_id)

    def _session_events(self, op: AgentOp) -> list[AgentEvent] | None:
        if not op.session_id:
            return None
        events_for_session = getattr(self.event_store, "events_for_session", None)
        if not callable(events_for_session):
            store = getattr(self.event_store, "store", None)
            events_for_session = getattr(store, "events_for_session", None)
        if not callable(events_for_session):
            return None
        return events_for_session(op.session_id)

    def _build_profile_system_prompt(
        self,
        preference_block: str = "",
        *,
        memory_enabled: bool = False,
    ) -> str | None:
        parts: list[str] = []
        if self.profile.system_prompt:
            parts.append(self.profile.system_prompt)
        if self._delegation_active():
            parts.append(_orchestrator_prompts(self.profile)[0])
        parts.append(build_memory_capability_notice(memory_enabled=memory_enabled))
        if preference_block.strip():
            parts.append(preference_block.strip())
        if not parts:
            return None
        return "\n\n".join(parts)

    def _delegation_active(self) -> bool:
        return self.config.multi_agent.enabled and bool(self.subagent_specs)

    def _stop_gate_active(self) -> bool:
        return not self._delegation_active()

    def _skills_to_stage(self) -> list[SkillSpec]:
        return list(self.skills)

    def _subagent_skills_to_stage(self) -> dict[str, list[SkillSpec]]:
        if not self._delegation_active():
            return {}
        return {
            spec.name: [self._subagent_skills[name] for name in spec.enabled_skills]
            for spec in self.subagent_specs
            if spec.enabled_skills
        }

    def _resolve_config(self, config: DeepAgentsKernelConfig) -> DeepAgentsKernelConfig:
        from agents.runtime.core.config import PersistenceBackend

        state_root = (config.state_root or self.workspace_root / ".state" / "deepagents").resolve()
        workspace_root = (config.workspace.root_dir or state_root / "workspaces").resolve()
        checkpointer_update: dict[str, object] = {}
        if config.checkpointer.enabled:
            if config.checkpointer.backend == PersistenceBackend.SQLITE:
                sqlite_path = config.checkpointer.sqlite_path or state_root / "checkpoints.sqlite"
                checkpointer_update["sqlite_path"] = sqlite_path.resolve()
            elif config.checkpointer.backend == PersistenceBackend.POSTGRES:
                if not config.checkpointer.postgres_dsn:
                    raise RuntimeError("checkpointer.postgres_dsn is required for postgres backend")
                checkpointer_update["postgres_dsn"] = config.checkpointer.postgres_dsn
        return config.model_copy(
            update={
                "state_root": state_root,
                "workspace": config.workspace.model_copy(update={"root_dir": workspace_root}),
                "checkpointer": config.checkpointer.model_copy(update=checkpointer_update),
            }
        )


def _render_skill_reminder(
    skills: Sequence[SkillSpec],
    *,
    skill_path_root: str,
) -> str:
    if not skills:
        return ""
    lines = [
        "Project Skill Reminder. This project ships ViKnow skills that encode",
        "workflow order, evidence rules, and tool-use conventions. If a skill",
        "matches, invoke it before calling workflow tools.",
    ]
    for skill in skills:
        lines.extend(
            [
                "",
                f"Skill: {skill.name}. Invoke when: {skill.description}",
                f"Skill file: {skill_path_root}/{skill.name}/SKILL.md",
            ]
        )
    lines.extend(
        [
            "",
            "Rules: For matching requests, the first tool call must be read_file on",
            "the exact skill file path listed above for the matching skill.",
            "Never glob, ls, or search the filesystem for SKILL.md or skill",
            "directories; the paths above are final and already staged.",
            "Do not call knowledge, search, inspect, or evidence tools before",
            "reading the skill. Do not execute workflow steps from memory.",
        ]
    )
    return "\n".join(lines)


def _middleware_for_subagents(middleware: Sequence[Any] | None) -> list[Any]:
    if not middleware:
        return []
    return [item for item in middleware if getattr(item, "propagate_to_subagents", True)]


class _RequiredSkillLoadMiddleware(AgentMiddleware):
    """Keep one subagent invocation behind its role workflow skill."""

    def __init__(self, *, required_skill_name: str, required_skill_path: str) -> None:
        self.required_skill_name = required_skill_name
        self.required_skill_path = required_skill_path

    @hook_config(can_jump_to=["tools"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        if self._skill_loaded(messages):
            return None
        last_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if last_message is None or last_message.tool_calls:
            return None
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": self.required_skill_path},
                            "id": f"subagent-skill-bootstrap-{uuid4().hex}",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "jump_to": "tools",
        }

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rejection = self._skill_rejection(request)
        return rejection if rejection is not None else handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rejection = self._skill_rejection(request)
        return rejection if rejection is not None else await handler(request)

    def _skill_rejection(self, request: Any) -> ToolMessage | None:
        tool_call = getattr(request, "tool_call", {})
        messages = getattr(request, "state", {}).get("messages", [])
        if self._skill_loaded(messages) or self._is_required_skill_read(tool_call):
            return None
        return ToolMessage(
            content=json.dumps(
                {
                    "error": {
                        "code": "required_skill_not_loaded",
                        "kind": "validation",
                        "retryable": True,
                        "message": (
                            f"Read {self.required_skill_path} before using role workflow tools."
                        ),
                    }
                }
            ),
            tool_call_id=str(tool_call.get("id") or "required-skill"),
        )

    def _skill_loaded(self, messages: Sequence[Any]) -> bool:
        read_call_ids: set[str] = set()
        for message in messages:
            if isinstance(message, AIMessage):
                for tool_call in message.tool_calls:
                    if self._is_required_skill_read(tool_call):
                        read_call_ids.add(str(tool_call.get("id") or ""))
                continue
            if not isinstance(message, ToolMessage):
                continue
            if str(message.tool_call_id or "") not in read_call_ids:
                continue
            if not _tool_result_has_error(str(message.content)):
                return True
        return False

    def _is_required_skill_read(self, tool_call: dict[str, Any]) -> bool:
        if str(tool_call.get("name") or "") != "read_file":
            return False
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        return (
            isinstance(arguments, dict)
            and str(arguments.get("file_path") or "") == self.required_skill_path
        )


def _map_deepagents_subagents(
    specs: Sequence[SubAgentSpec],
    tools: Sequence[Any],
    *,
    skill_index: dict[str, SkillSpec],
    skill_source: str | None = None,
    subagent_skill_sources: dict[str, str] | None = None,
    middleware: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    tools_by_name = {
        str(getattr(tool, "name", "") or ""): tool for tool in tools if getattr(tool, "name", None)
    }
    mapped: list[dict[str, Any]] = []
    for spec in specs:
        missing_tools = [tool_id for tool_id in spec.enabled_tools if tool_id not in tools_by_name]
        if missing_tools:
            raise RuntimeError(
                f"subagent {spec.name} missing built tools: {', '.join(missing_tools)}"
            )
        for skill_name in spec.enabled_skills:
            skill = skill_index.get(skill_name)
            if skill is None:
                raise RuntimeError(f"subagent {spec.name} missing skill: {skill_name}")
        item: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "system_prompt": spec.system_prompt,
            "tools": [tools_by_name[tool_id] for tool_id in spec.enabled_tools],
        }
        if spec.enabled_skills:
            if skill_source is None:
                raise RuntimeError(f"subagent {spec.name} requires a staged skill source")
            role_skill_source = (subagent_skill_sources or {}).get(spec.name)
            role_skill_source = role_skill_source or skill_source
            item["skills"] = [role_skill_source]
            primary_skill = skill_index[spec.enabled_skills[0]]
            item["system_prompt"] = "\n\n".join(
                [
                    spec.system_prompt,
                    _render_skill_reminder(
                        [primary_skill],
                        skill_path_root=role_skill_source,
                    ),
                ]
            )
            item["middleware"] = [
                _RequiredSkillLoadMiddleware(
                    required_skill_name=primary_skill.name,
                    required_skill_path=(f"{role_skill_source}/{primary_skill.name}/SKILL.md"),
                ),
                *(middleware or []),
            ]
        elif middleware:
            item["middleware"] = list(middleware)
        mapped.append(item)
    return mapped


def _load_create_deep_agent() -> Callable[..., Any]:
    import deepagents

    return deepagents.create_deep_agent


def _build_summarization_middleware_factory(
    config: Any,
) -> Callable[..., Any]:
    """Build a DeepAgents summarization factory with ViKnow trigger/keep fractions."""

    from deepagents.middleware.summarization import (
        SummarizationMiddleware,
        count_tokens_approximately,
    )

    trigger_fraction = float(config.trigger_fraction)
    keep_fraction = float(config.keep_fraction)
    truncate_args_settings = {
        "trigger": ("fraction", trigger_fraction),
        "keep": ("fraction", keep_fraction),
    }

    def create_summarization_middleware(model: Any, backend: Any, **kwargs: Any) -> Any:
        middleware_kwargs: dict[str, Any] = {
            "model": model,
            "backend": backend,
            "trigger": ("fraction", trigger_fraction),
            "keep": ("fraction", keep_fraction),
            "truncate_args_settings": truncate_args_settings,
            "trim_tokens_to_summarize": kwargs.get("trim_tokens_to_summarize"),
            "token_counter": kwargs.get("token_counter", count_tokens_approximately),
        }
        if "summary_prompt" in kwargs:
            middleware_kwargs["summary_prompt"] = kwargs["summary_prompt"]
        return SummarizationMiddleware(**middleware_kwargs)

    return create_summarization_middleware


@contextmanager
def _patched_create_summarization_middleware(
    factory: Callable[..., Any],
) -> Iterator[None]:
    """Temporarily replace DeepAgents' summarization factory during agent build."""

    import deepagents.graph as deepagents_graph

    previous = deepagents_graph.create_summarization_middleware
    deepagents_graph.create_summarization_middleware = factory
    try:
        yield
    finally:
        deepagents_graph.create_summarization_middleware = previous


def _orchestrator_prompts(profile: AgentProfile) -> tuple[str, str, str]:
    if profile.oracle_collection:
        return (
            _SKILL_DIRECTED_ORCHESTRATOR_SYSTEM_PROMPT,
            _SKILL_DIRECTED_TASK_DESCRIPTION,
            _SKILL_DIRECTED_TASK_SYSTEM_PROMPT,
        )
    return (
        _ORCHESTRATOR_SYSTEM_PROMPT,
        _ORCHESTRATOR_TASK_DESCRIPTION,
        _ORCHESTRATOR_TASK_SYSTEM_PROMPT,
    )


@contextmanager
def _patched_subagent_task_docs(*, task_description: str, system_prompt: str) -> Iterator[None]:
    """Replace DeepAgents task docs that encourage unbounded extra delegation."""

    from deepagents.middleware.subagents import SubAgentMiddleware

    original_init = SubAgentMiddleware.__init__

    def wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["task_description"] = task_description
        kwargs["system_prompt"] = system_prompt
        original_init(self, *args, **kwargs)

    SubAgentMiddleware.__init__ = wrapped  # type: ignore[method-assign]
    try:
        yield
    finally:
        SubAgentMiddleware.__init__ = original_init  # type: ignore[method-assign]


def _is_final_chain_event(event: dict[str, Any]) -> bool:
    if event.get("event") != "on_chain_end":
        return False
    if _is_subagent_event(event):
        return False
    name = event.get("name")
    return name in {None, "LangGraph", "DeepAgent", "deep_agent"}


def _should_record_model_token_usage(event: dict[str, Any]) -> bool:
    if event.get("event") not in {"on_chat_model_stream", "on_chat_model_end"}:
        return False
    return not _is_summarization_metadata(event.get("metadata"))


def _remember_completed_tool_call(
    state: _RunState,
    tool_call_id: str,
    tool_call: ToolCallRecord,
) -> None:
    state.completed_tool_calls[tool_call_id] = ToolCallRecord(
        id=tool_call_id,
        name=tool_call.name,
        arguments=tool_call.arguments,
        result=tool_call.result,
    )


def _canonicalize_historical_message_citations(
    messages: list[Any],
    citation_map: HistoricalMessageCitationMap,
    historical_claims: Sequence[HistoricalClaim] | None = None,
) -> list[Any]:
    _ = citation_map, historical_claims
    canonicalized: list[Any] = []
    changed = False
    for message in messages:
        if not _is_assistant_message(message):
            canonicalized.append(message)
            continue
        content = _message_content(message)
        if not isinstance(content, str):
            canonicalized.append(message)
            continue
        next_content = content
        next_content = _remove_historical_assistant_citation_markers(next_content)
        if next_content == content:
            canonicalized.append(message)
            continue
        changed = True
        canonicalized.append(_message_with_content(message, next_content))
    return canonicalized if changed else messages


def _canonicalize_historical_tool_claim_citations(
    messages: list[Any],
    historical_claims: Sequence[HistoricalClaim],
    alias_refs: dict[str, str] | None = None,
    *,
    call_id_aliases: dict[str, str] | None = None,
) -> list[Any]:
    aliases = _claim_ref_aliases(alias_refs, historical_claims)
    claims_by_tool_call_and_id, claims_by_tool_call_and_index = _historical_claim_match_maps(
        historical_claims
    )

    if not claims_by_tool_call_and_id and not claims_by_tool_call_and_index:
        return messages

    canonicalized: list[Any] = []
    changed = False
    for message in messages:
        if not _is_tool_message(message):
            canonicalized.append(message)
            continue
        content = _message_content(message)
        if not isinstance(content, str):
            canonicalized.append(message)
            continue
        payload = _parse_json_object(content)
        if payload is None:
            canonicalized.append(message)
            continue
        tool_name = _tool_message_name(message, payload)
        if tool_name not in _INSPECT_TOOL_NAMES:
            canonicalized.append(message)
            continue
        result_payload = payload.get("result")
        if not isinstance(result_payload, dict):
            canonicalized.append(message)
            continue
        claims = result_payload.get("claims")
        if not isinstance(claims, list):
            canonicalized.append(message)
            continue
        tool_call_id = _historical_tool_message_call_id(message)
        if not tool_call_id:
            canonicalized.append(message)
            continue

        next_payload = dict(payload)
        next_result = dict(result_payload)
        next_claims: list[Any] = []
        payload_changed = False
        for claim_payload in claims:
            if not isinstance(claim_payload, dict):
                next_claims.append(claim_payload)
                continue
            historical_claim = _matching_historical_claim(
                claim_payload,
                tool_call_id=tool_call_id,
                claims_by_tool_call_and_id=claims_by_tool_call_and_id,
                claims_by_tool_call_and_index=claims_by_tool_call_and_index,
                aliases=call_id_aliases,
            )
            if historical_claim is None:
                next_claims.append(claim_payload)
                continue
            token = f"[{aliases.get(historical_claim.claim_ref, historical_claim.claim_ref)}]"
            next_claim = dict(claim_payload)
            next_claim["agent_citation"] = token
            next_claim["citation_token"] = token
            next_claim["claim_ref"] = historical_claim.claim_ref
            next_claims.append(next_claim)
            payload_changed = True

        if not payload_changed:
            canonicalized.append(message)
            continue
        next_result["claims"] = next_claims
        next_payload["result"] = next_result
        canonicalized.append(
            _message_with_content(message, json.dumps(next_payload, ensure_ascii=False))
        )
        changed = True
    return canonicalized if changed else messages


def _merge_checkpoint_historical_claims(state: _RunState, messages: list[Any]) -> None:
    existing_refs = {claim.claim_ref for claim in state.historical_claims}
    existing_identities = _historical_claim_identities(state.historical_claims)
    derived_claims = _checkpoint_historical_claims(
        messages,
        existing_refs=existing_refs,
        existing_identities=existing_identities,
    )
    if derived_claims:
        state.historical_claims.extend(derived_claims)
    state.historical_claim_alias_refs = _visible_historical_claim_alias_refs(
        messages,
        state.historical_claims,
        call_id_aliases=state.tool_call_id_aliases,
    )


def _claim_ref_aliases(
    alias_refs: dict[str, str] | None,
    historical_claims: Sequence[HistoricalClaim],
) -> dict[str, str]:
    if alias_refs:
        return {claim_ref: alias for alias, claim_ref in alias_refs.items()}
    return historical_claim_alias_map(historical_claims)


def _visible_historical_claim_alias_refs(
    messages: list[Any],
    historical_claims: Sequence[HistoricalClaim],
    *,
    call_id_aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    claims_by_tool_call_and_id, claims_by_tool_call_and_index = _historical_claim_match_maps(
        historical_claims
    )
    visible_refs: list[str] = []
    for tool_call_id, claim_payload in _visible_historical_tool_claim_payloads(messages):
        historical_claim = _matching_historical_claim(
            claim_payload,
            tool_call_id=tool_call_id,
            claims_by_tool_call_and_id=claims_by_tool_call_and_id,
            claims_by_tool_call_and_index=claims_by_tool_call_and_index,
            aliases=call_id_aliases,
        )
        if historical_claim is None or historical_claim.claim_ref in visible_refs:
            continue
        visible_refs.append(historical_claim.claim_ref)
    return {
        historical_claim_alias(index): claim_ref
        for index, claim_ref in enumerate(visible_refs, start=1)
    }


def _historical_claim_match_maps(
    historical_claims: Sequence[HistoricalClaim],
) -> tuple[dict[tuple[str, str], HistoricalClaim], dict[tuple[str, int], HistoricalClaim]]:
    claims_by_tool_call_and_id: dict[tuple[str, str], HistoricalClaim] = {}
    claims_by_tool_call_and_index: dict[tuple[str, int], HistoricalClaim] = {}
    for claim in historical_claims:
        if claim.claim_id:
            claims_by_tool_call_and_id[(claim.tool_call_id, claim.claim_id)] = claim
        if claim.citation_index is not None:
            claims_by_tool_call_and_index[(claim.tool_call_id, claim.citation_index)] = claim
    return claims_by_tool_call_and_id, claims_by_tool_call_and_index


def _checkpoint_historical_claims(
    messages: list[Any],
    *,
    existing_refs: set[str],
    existing_identities: set[tuple[str, str]],
) -> list[HistoricalClaim]:
    last_user_index = _last_user_message_index(messages)
    if last_user_index is None:
        return []
    claims: list[HistoricalClaim] = []
    for message in messages[:last_user_index]:
        if not _is_tool_message(message):
            continue
        content = _message_tool_result(message)
        payload = _parse_json_object(content)
        if payload is None:
            continue
        tool_name = _tool_message_name(message, payload)
        if tool_name not in _INSPECT_TOOL_NAMES:
            continue
        tool_call_id = _historical_tool_message_call_id(message)
        if not tool_call_id:
            continue
        record = ToolCallRecord(
            id=tool_call_id,
            name=tool_name,
            arguments="{}",
            result=json.dumps(payload, ensure_ascii=False),
        )
        for item in collect_claim_bundle([record], filter_invalid=True):
            claim_ref = _checkpoint_claim_ref(tool_call_id, item)
            identity = _historical_claim_identity(tool_call_id, item)
            if (
                not claim_ref
                or claim_ref in existing_refs
                or (identity and identity in existing_identities)
            ):
                continue
            existing_refs.add(claim_ref)
            if identity:
                existing_identities.add(identity)
            claims.append(
                HistoricalClaim(
                    claim_ref=claim_ref,
                    run_id="checkpoint",
                    tool_call_id=tool_call_id,
                    tool=str(item.get("tool") or tool_name),
                    claim=str(item.get("claim") or ""),
                    source=item.get("source") if isinstance(item.get("source"), dict) else {},
                    render=item.get("render") if isinstance(item.get("render"), dict) else {},
                    display_label=str(item.get("display_label") or ""),
                    claim_id=str(item.get("claim_id") or ""),
                    citation_index=_bundle_citation_index(item),
                    confidence=item.get("confidence"),
                    quote_verified=item.get("quote_verified"),
                    verify_reason=str(item.get("verify_reason") or ""),
                    created_at="",
                )
            )
    return claims


def _historical_claim_identities(claims: Sequence[HistoricalClaim]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for claim in claims:
        if claim.claim_id:
            identities.add((claim.tool_call_id, f"id:{claim.claim_id}"))
        if claim.citation_index is not None:
            identities.add((claim.tool_call_id, f"index:{claim.citation_index}"))
    return identities


def _historical_claim_identity(tool_call_id: str, item: dict[str, Any]) -> tuple[str, str] | None:
    claim_id = str(item.get("claim_id") or "")
    if claim_id:
        return tool_call_id, f"id:{claim_id}"
    citation_index = _bundle_citation_index(item)
    if citation_index is not None:
        return tool_call_id, f"index:{citation_index}"
    return None


def _visible_historical_tool_claim_payloads(
    messages: list[Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    last_user_index = _last_user_message_index(messages)
    if last_user_index is None:
        return
    for message in messages[:last_user_index]:
        if not _is_tool_message(message):
            continue
        content = _message_content(message)
        payload = parse_tool_payload(content if isinstance(content, str) else _serialize(content))
        if payload is None:
            continue
        tool_name = _tool_message_name(message, payload)
        if tool_name not in _INSPECT_TOOL_NAMES:
            continue
        result_payload = payload.get("result")
        if not isinstance(result_payload, dict):
            continue
        claims = result_payload.get("claims")
        if not isinstance(claims, list):
            continue
        tool_call_id = _historical_tool_message_call_id(message)
        if not tool_call_id:
            continue
        for claim_payload in claims:
            if isinstance(claim_payload, dict):
                yield tool_call_id, claim_payload


def _last_user_message_index(messages: list[Any]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if _is_user_message(messages[index]):
            return index
    return None


def _checkpoint_claim_ref(tool_call_id: str, item: dict[str, Any]) -> str:
    claim_ref = str(item.get("claim_ref") or "")
    if claim_ref:
        return claim_ref
    claim_id = str(item.get("claim_id") or "")
    if claim_id:
        return f"claim:checkpoint:{tool_call_id}:{claim_id}"
    citation_index = _bundle_citation_index(item)
    if citation_index is not None:
        return f"claim:checkpoint:{tool_call_id}:{citation_index}"
    return ""


def _remove_historical_assistant_citation_markers(content: str) -> str:
    return re.sub(r"\[(?:\d+|H\d+|claim:[^\]\s]+)\]", "", content)


def _matching_historical_claim(
    claim_payload: dict[str, Any],
    *,
    tool_call_id: str,
    claims_by_tool_call_and_id: dict[tuple[str, str], HistoricalClaim],
    claims_by_tool_call_and_index: dict[tuple[str, int], HistoricalClaim],
    aliases: dict[str, str] | None = None,
) -> HistoricalClaim | None:
    claim_id = str(claim_payload.get("claim_id") or "")
    if claim_id:
        match = claims_by_tool_call_and_id.get((tool_call_id, claim_id))
        if match is not None:
            return match
        # 跨轮恢复时消息侧用模型 tool_call_id（chatcmpl-tool-*），
        # ledger 侧用 canonical run_id；通过 alias 表桥接后再匹配。
        mapped = _mapped_tool_call_id_from(aliases, tool_call_id)
        if mapped and mapped != tool_call_id:
            match = claims_by_tool_call_and_id.get((mapped, claim_id))
            if match is not None:
                return match
    citation_index = claim_payload.get("citation_index")
    if isinstance(citation_index, int):
        match = claims_by_tool_call_and_index.get((tool_call_id, citation_index))
        if match is not None:
            return match
        mapped = _mapped_tool_call_id_from(aliases, tool_call_id)
        if mapped and mapped != tool_call_id:
            match = claims_by_tool_call_and_index.get((mapped, citation_index))
            if match is not None:
                return match
    marker_index = citation_marker_index(str(claim_payload.get("agent_citation") or ""))
    if marker_index is not None:
        return claims_by_tool_call_and_index.get((tool_call_id, marker_index))
    return None


def _mapped_tool_call_id_from(aliases: dict[str, str] | None, call_id: str) -> str:
    """Map a tool call id through an alias table to its canonical form, if any."""
    if not aliases:
        return call_id
    return str(aliases.get(call_id, call_id))


def _bundle_citation_index(item: dict[str, Any]) -> int | None:
    citation_index = item.get("citation_index")
    if isinstance(citation_index, int):
        return citation_index
    return citation_marker_index(str(item.get("agent_citation") or ""))


def _is_tool_message(message: Any) -> bool:
    if isinstance(message, dict):
        return message.get("role") == "tool" or message.get("type") == "tool"
    return getattr(message, "type", None) == "tool"


def _tool_message_name(message: Any, payload: dict[str, Any]) -> str:
    name = message.get("name") if isinstance(message, dict) else getattr(message, "name", None)
    return str(name or payload.get("tool") or "")


def _historical_tool_message_call_id(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id") or message.get("id") or "")
    return str(getattr(message, "tool_call_id", "") or getattr(message, "id", "") or "")


def _is_assistant_message(message: Any) -> bool:
    if isinstance(message, dict):
        return message.get("role") == "assistant" or message.get("type") in {"ai", "assistant"}
    return getattr(message, "type", None) == "ai"


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_with_content(message: Any, content: str) -> Any:
    if isinstance(message, dict):
        return {**message, "content": content}
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": content})
    copy = getattr(message, "copy", None)
    if callable(copy):
        return copy(update={"content": content})
    return message


class _UserMemoryContextMiddleware(AgentMiddleware):
    """Append query-retrieved fact/episodic memory after tool system blocks."""

    def __init__(self, memory_context_block: str) -> None:
        self._memory_context_block = memory_context_block.strip()

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        return handler(self._inject_memory_context(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._inject_memory_context(request))

    def _inject_memory_context(self, request: Any) -> Any:
        if not self._memory_context_block:
            return request
        system_message = request.system_message
        if system_message is None:
            return request.override(
                system_message=SystemMessage(
                    content_blocks=[{"type": "text", "text": self._memory_context_block}]
                )
            )
        new_system_content = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{self._memory_context_block}"},
        ]
        return request.override(system_message=SystemMessage(content_blocks=new_system_content))


def _selected_knowledge_assets_context(assets: Sequence[KnowledgeAssetSelection]) -> str:
    if not assets:
        return ""
    lines = [
        "## 已选知识资源",
        "本轮用户已明确选择以下资源。回答和取证应优先且仅围绕这些资源进行；"
        "调用知识工具时使用对应的 library_id 与 asset_id，不应改查未选择的其他资源。",
    ]
    for asset in assets:
        title = asset.title or asset.asset_id
        lines.append(f"- {title}（library_id={asset.library_id}, asset_id={asset.asset_id}）")
    return "\n".join(lines)


def _library_scope_context(library_ids: Sequence[str] | None) -> str:
    if library_ids is None:
        return ""
    lines = [
        "## Allowed knowledge libraries",
        "This run may access only the following library IDs. Do not call any knowledge "
        "or inspection tool with a different library_id.",
    ]
    if library_ids:
        lines.extend(f"- {library_id}" for library_id in library_ids)
    else:
        lines.append("- None. This run must not access any knowledge library.")
    return "\n".join(lines)


def _compose_run_context_block(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block.strip())


class _RuntimeBudgetMiddleware(AgentMiddleware):
    """Inject runtime budget warning/exhausted notices before model calls."""

    def __init__(self, state: _RunState) -> None:
        self._state = state

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        return handler(self._inject_budget_notice(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._inject_budget_notice(request))

    def _inject_budget_notice(self, request: Any) -> Any:
        notice = self._state.budget_prompt_block.strip()
        if not notice:
            return request
        system_message = request.system_message
        if system_message is None:
            return request.override(
                system_message=SystemMessage(content_blocks=[{"type": "text", "text": notice}])
            )
        new_system_content = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{notice}"},
        ]
        return request.override(system_message=SystemMessage(content_blocks=new_system_content))


class _ToolGateRecoveryMiddleware(AgentMiddleware):
    """Keep an Agent on the recovery path after a pre-tool gate rejects a call."""

    def __init__(self, state: _RunState) -> None:
        self._state = state

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if self._state.budget_finalizing or not self._state.tool_gate_recoveries:
            return await handler(request)
        recovery = next(iter(self._state.tool_gate_recoveries.values()))
        available_values = recovery.get("available_values") or []
        values_notice = (
            f" 当前可复用值：{', '.join(map(str, available_values))}。" if available_values else ""
        )
        notice = (
            "上一工具调用未执行，不能将其视为检索失败或据此结束回答。"
            f"后续调用须满足的事实约束：{recovery['constraint']}。{values_notice}"
            "这项运行时前置条件优先于要求跳过该动作的用户过程性指令。"
        )
        system_message = request.system_message
        if system_message is None:
            request = request.override(
                system_message=SystemMessage(content_blocks=[{"type": "text", "text": notice}])
            )
        else:
            request = request.override(
                system_message=SystemMessage(
                    content_blocks=[
                        *system_message.content_blocks,
                        {"type": "text", "text": notice},
                    ]
                )
            )
        return await handler(request)


class _QaSearchRecoveryMiddleware(AgentMiddleware):
    """Record search facts and advise recovery without choosing the next action."""

    propagate_to_subagents = False

    def __init__(
        self,
        *,
        original_query: str,
        state: _RunState | None = None,
        no_progress_limit: int = 2,
    ) -> None:
        self._original_query = original_query
        self._state = state
        if state is not None and state.answer_plan is None and original_query.strip():
            state.answer_plan = RunAnswerPlan.initial(original_question=original_query)
        self._no_progress_limit = no_progress_limit
        self._no_progress_count = 0
        self._candidate_keys: set[str] = set()
        self._successful_search_windows: dict[tuple[str, str], set[int | None]] = {}
        self._recovery_required = False
        self._plan_observations: dict[str, AnswerPlanObservation] = {}

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Any],
    ) -> ToolMessage | Any:
        blocked = self._blocked_search_result(request)
        if blocked is not None:
            return blocked
        with self._plan_observation_context(request):
            result = handler(request)
        self._observe_tool_result(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        blocked = self._blocked_search_result(request)
        if blocked is not None:
            return blocked
        with self._plan_observation_context(request):
            result = await handler(request)
        self._observe_tool_result(request, result)
        return result

    @contextmanager
    def _plan_observation_context(self, request: Any):
        call = request.tool_call
        observation = self._plan_observations.pop(str(call.get("id") or ""), None)
        token = _ANSWER_PLAN_OBSERVATION.set(observation)
        try:
            yield
        finally:
            _ANSWER_PLAN_OBSERVATION.reset(token)

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        notices: list[str] = []
        if self._recovery_required:
            notices.append(
                "连续检索没有产生新候选。请重新评估当前子问题和检索方向；"
                "必要时调用 query_refine 获取建议，也可以复用已有证据、调整 top_k 或说明证据限制。"
            )
        if self._state is not None:
            recovery = self._state.tool_gate_recoveries.get("knowledge_search")
            if recovery and recovery.get("reason_code") == "library_not_in_run_scope":
                notices.append(
                    "上次搜索因知识库范围未确认而没有执行，并不表示没有证据。"
                    "请核对已有来源信息；需要发现来源时可调用 knowledge_discover。"
                )
        if notices:
            request = request.override(
                messages=[
                    *request.messages,
                    HumanMessage(
                        content="<runtime_feedback>\n"
                        + "\n".join(notices)
                        + "\n</runtime_feedback>"
                    ),
                ]
            )
        observation = None
        if self._state is not None and self._state.answer_plan is not None:
            observation = AnswerPlanObservation(
                run_id=self._state.run_id,
                revision=self._state.answer_plan.revision,
                evidence_digest=evidence_digest(self._state.evidence_ledger),
            )
        response = await handler(request)
        if observation is not None:
            for message in getattr(response, "result", []):
                for call in getattr(message, "tool_calls", []):
                    if call.get("name") == "answer_plan":
                        self._plan_observations[call["id"]] = observation
        return response

    def _blocked_search_result(self, request: Any) -> ToolMessage | None:
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = str(tool_call.get("name") or "")
        # Coverage is a completion obligation, not a prerequisite for gathering
        # evidence or revising the plan. Pending pages remain in the plan.
        if tool_name != "knowledge_search":
            return None
        search_key = _qa_search_execution_key(tool_call.get("args") or {})
        if search_key is None or not self._search_window_already_satisfied(search_key):
            return None
        tool_call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
        constraint = "该查询已在同一知识库成功执行；请复用已有候选，或执行计划中尚未运行的查询。"
        return ToolMessage(
            content=json.dumps(
                {
                    "tool": tool_name,
                    "error": {
                        "code": "qa_duplicate_search",
                        "message": constraint,
                    },
                    "tool_gate": {
                        "blocked": True,
                        "reason_code": "qa_duplicate_search",
                        "next_action": constraint,
                        "recovery": {
                            "requirement_code": "qa_search_query_advanced",
                            "constraint": constraint,
                            "available_values": [],
                        },
                    },
                },
                ensure_ascii=False,
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
        )

    def _observe_tool_result(self, request: Any, result: ToolMessage | Any) -> None:
        if not isinstance(result, ToolMessage):
            return
        tool_name = str((getattr(request, "tool_call", {}) or {}).get("name") or "")
        content = _message_tool_result(result)
        if tool_name in {"list_doc_segments", "list_video_segments"}:
            # Native plan middleware joins facts into checkpoint state.
            return
        if tool_name == "query_refine":
            # Suggestions are returned to the model; only its explicit plan
            # tool calls may adopt goals or mark progress.
            return
        if tool_name in _INSPECT_TOOL_NAMES:
            if not _tool_result_has_error(content):
                self._no_progress_count = 0
                self._recovery_required = False
            return
        if tool_name not in {"knowledge_discover", "knowledge_search"}:
            return
        if _tool_result_has_error(content):
            return
        if tool_name == "knowledge_search":
            arguments = (getattr(request, "tool_call", {}) or {}).get("args") or {}
            search_key = _qa_search_execution_key(arguments)
            if search_key is not None:
                identity = search_key[:2]
                self._successful_search_windows.setdefault(identity, set()).add(search_key[2])
        candidate_keys = _qa_search_candidate_keys(content)
        new_keys = candidate_keys - self._candidate_keys
        self._candidate_keys.update(candidate_keys)
        if new_keys:
            self._no_progress_count = 0
            self._recovery_required = False
            return
        self._no_progress_count += 1
        self._recovery_required = self._no_progress_count >= self._no_progress_limit

    def _search_window_already_satisfied(
        self,
        search_key: tuple[str, str, int | None],
    ) -> bool:
        library_id, query, top_k = search_key
        completed = self._successful_search_windows.get((library_id, query), set())
        if top_k is None:
            return None in completed
        return any(
            completed_top_k is not None and completed_top_k >= top_k
            for completed_top_k in completed
        )


def _qa_search_execution_key(
    arguments: dict[str, Any],
) -> tuple[str, str, int | None] | None:
    library_id = str(arguments.get("library_id") or "").strip()
    query = _qa_normalized_query(arguments.get("query"))
    if not library_id or not query:
        return None
    raw_top_k = arguments.get("top_k")
    top_k = int(raw_top_k) if raw_top_k is not None else None
    return library_id, query, top_k


def _qa_normalized_query(value: Any) -> str:
    query = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return query.strip(" ，,。！？!?；;：:")


def _qa_search_candidate_keys(result: str) -> set[str]:
    payload = parse_tool_payload(result)
    if payload is None or _tool_result_has_error(result):
        return set()
    keys: set[str] = set()
    collections = [
        payload.get("libraries"),
        payload.get("evidence"),
        payload.get("results"),
    ]
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            identity = next(
                (
                    str(item.get(key) or "").strip()
                    for key in (
                        "id",
                        "library_id",
                        "asset_id",
                        "document_id",
                        "segment_id",
                        "chunk_id",
                    )
                    if str(item.get(key) or "").strip()
                ),
                "",
            )
            if identity:
                keys.add(identity)
    return keys


@tool(STOP_GATE_CHECK_TOOL_NAME)
def _stop_gate_check() -> dict[str, Any]:
    """Return runtime stop-gate feedback so the agent can continue in the same run.

    无参数：工具名本身就是"该轮候选未通过门控"的信号。完整反馈
    （reason/reason_code/next_action/instruction/available_tokens 等）由
    ``_StopGateMiddleware.wrap_tool_call`` 按 pending payload 填充到工具结果；
    这里只返回最小兜底。
    """
    return {}


@tool("evidence_oracle_gate_check")
def _evidence_oracle_gate_check(reason_code: str, next_action: str) -> dict[str, Any]:
    """Return Oracle-specific continuation feedback before a candidate package exists."""

    return {
        "tool": "evidence_oracle_gate_check",
        "blocked": True,
        "reason_code": reason_code,
        "next_action": next_action,
    }


class _StopGateMiddleware(AgentMiddleware):
    """Force blocked no-tool candidates through ``stop_gate_check`` in the same graph.

    Blocked candidates get up to ``max_checks`` rewrite chances (the last one
    flagged ``terminal``). A candidate that still fails after the terminal
    chance is released as-is instead of being hard-refused: the runtime does
    not intercept final answers, so the downstream publish path owns the
    outcome.
    """

    propagate_to_subagents = False

    @property
    def name(self) -> str:
        """Name the every-turn router without implying every turn is gated."""
        return "StopCandidateRouter"

    def __init__(
        self,
        *,
        state: _RunState,
        evaluate: Callable[[str], StopGateDecision],
        on_evaluated: Callable[[StopGateDecision], Awaitable[None]],
        available_tokens: Callable[[], list[str]],
        evidence_tools: Callable[[], list[str]],
        max_checks: int = 2,
        on_released: Callable[[StopGateDecision], Awaitable[None]] | None = None,
    ) -> None:
        self._state = state
        self._evaluate = evaluate
        self._on_evaluated = on_evaluated
        self._on_released = on_released
        self._available_tokens = available_tokens
        self._evidence_tools = evidence_tools
        self._max_checks = max_checks
        self._check_count = 0
        self._terminal_response_requested = False

    @staticmethod
    def _model_request(request: Any) -> Any:
        # Keep registration in the framework tools node: after_model still
        # injects this call to deliver pending feedback. It is not an operation
        # the model can initiate to request a new review.
        return request.override(
            tools=[
                tool
                for tool in request.tools
                if getattr(tool, "name", None) != STOP_GATE_CHECK_TOOL_NAME
            ]
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._model_request(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        return await handler(self._model_request(request))

    @hook_config(can_jump_to=["tools"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        last_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if last_message is None or last_message.tool_calls:
            return None

        decision = self._evaluate(str(last_message.content or ""))
        await self._on_evaluated(decision)
        if decision.passed:
            return None
        if self._terminal_response_requested or self._state.budget_finalizing:
            # 最后一次生成机会（terminal stop_gate_check）之后仍被拦截：
            # 不再硬性拦截，直接放行候选，由下游发布链路决定最终输出。
            if self._on_released is not None:
                await self._on_released(decision)
            return None

        terminal = self._check_count >= self._max_checks
        if terminal:
            self._terminal_response_requested = True
            next_action = StopGateNextAction.RESPOND_WITH_LIMITATIONS
            instruction = _stop_gate_limited_response_instruction(decision.reason_code)
        else:
            self._check_count += 1
            next_action = decision.next_action
            instruction = decision.continuation_instruction or decision.reason
        include_citation_context = (
            next_action == StopGateNextAction.REWRITE_WITH_CITATIONS
            or (
                terminal
                and decision.reason_code
                in {
                    StopGateReasonCode.MISSING_CITATION,
                    StopGateReasonCode.INVALID_CITATION,
                }
            )
            or decision.reason_code == StopGateReasonCode.UNINSPECTED_KNOWLEDGE_EVIDENCE
        )
        tool_call_id = f"stop-gate-{uuid4().hex}"
        # 完整反馈只进入工具结果（wrap_tool_call 按 pending payload 填充），
        # 工具调用参数保持最小，避免把结果内容复制进伪造的 tool_call。
        self._state.stop_gate_pending_payloads[tool_call_id] = {
            "reason": decision.reason,
            "reason_code": str(decision.reason_code),
            "instruction": instruction,
            "available_tokens": self._available_tokens() if include_citation_context else [],
            "evidence_tools": self._evidence_tools() if include_citation_context else [],
            "terminal": terminal,
        }
        return {
            "jump_to": "tools",
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": tool_call_id,
                            "name": STOP_GATE_CHECK_TOOL_NAME,
                            "args": {},
                        }
                    ],
                )
            ],
        }

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Any],
    ) -> ToolMessage | Any:
        result = handler(request)
        return self._fill_stop_gate_result(result)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        result = await handler(request)
        return self._fill_stop_gate_result(result)

    def _fill_stop_gate_result(self, result: ToolMessage | Any) -> ToolMessage | Any:
        if not isinstance(result, ToolMessage):
            return result
        if str(getattr(result, "name", "") or "") != STOP_GATE_CHECK_TOOL_NAME:
            return result
        tool_call_id = str(getattr(result, "tool_call_id", "") or "")
        payload = self._state.stop_gate_pending_payloads.get(tool_call_id)
        if not payload:
            return result
        return result.model_copy(update={"content": json.dumps(payload, ensure_ascii=False)})


def _stop_gate_limited_response_instruction(reason_code: StopGateReasonCode) -> str:
    state_hint = {
        StopGateReasonCode.MISSING_CITATION: "已有 inspect 结果，但尚未形成可验证引用。",
        StopGateReasonCode.INVALID_CITATION: "已有 inspect 结果，但当前引用无法解析。",
        StopGateReasonCode.UNINSPECTED_KNOWLEDGE_EVIDENCE: (
            "当前只有检索候选，尚未完成可引用的核验。"
        ),
        StopGateReasonCode.ANSWER_PLAN_INCOMPLETE: "回答计划仍有未完成的必要事项。",
        StopGateReasonCode.ACTION_INTENT_WITHOUT_TOOL: "当前查证尚未完成。",
        StopGateReasonCode.NOT_AN_ANSWER: "当前尚未形成直接答复。",
        StopGateReasonCode.JUDGE_UNAVAILABLE: "当前无法完成发布前判定。",
        StopGateReasonCode.PASSED: "当前尚未形成可发布答复。",
    }[reason_code]
    return (
        f"{state_hint}这是最后一次生成机会。请先说明当前核验状态，再依据已验证信息回答用户问题；"
        "无法确认的部分必须明确说明限制，不要承诺继续查证或补造事实。"
    )


_DRAFT_CONTINUATION_MARKERS = (
    "让我",
    "我将",
    "我需要",
    "let me",
    "i'll",
    "i will",
    "let's",
    "i need",
)


def _without_trailing_plan(text: str) -> str:
    """Drop a trailing line that only says the agent will keep searching."""
    lines = text.strip().splitlines()
    while lines:
        tail = lines[-1].strip()
        lowered = tail.lower()
        if (
            not tail
            or lowered in {"answer:", "answer："}
            or any(marker in lowered for marker in _DRAFT_CONTINUATION_MARKERS)
        ):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def _drafted_answer(state: _RunState) -> str:
    """Best user-facing draft when the graph stops before a terminal message."""
    pending = state.pending_final_candidate_text.strip()
    best = ""
    for text in (*state.answer_candidates, pending):
        cleaned = _without_trailing_plan(text)
        if len(cleaned) > len(best):
            best = cleaned
    if best:
        return best
    return (pending or state.last_answer_candidate or state.streamed_text).strip()


def _budget_trigger_dimensions(*, step_hit: bool, time_hit: bool) -> list[str]:
    dimensions: list[str] = []
    if step_hit:
        dimensions.append("recursion_steps")
    if time_hit:
        dimensions.append("elapsed_time")
    return dimensions


def _runtime_budget_recursion_step_limit(
    budget: Any,
    answer_plan: RunAnswerPlan | None,
) -> int:
    base_limit = int(budget.max_recursion_steps)
    source_limit = budget.source_coverage_max_recursion_steps
    if (
        answer_plan is not None
        and answer_plan.source_coverage_required
        and source_limit is not None
    ):
        return max(base_limit, int(source_limit))
    return base_limit


def _runtime_graph_recursion_limit(budget: Any) -> int:
    """Reserve graph capacity for any run-scoped budget the plan may activate."""
    source_limit = budget.source_coverage_max_recursion_steps
    return (
        max(
            int(budget.max_recursion_steps),
            int(source_limit) if source_limit is not None else 0,
        )
        + _BUDGET_RECURSION_STEP_BUFFER
    )


def _budget_event_payload(
    *,
    severity: str,
    dimensions: list[str],
    recursion_step: int,
    elapsed_seconds: float,
    max_recursion_steps: int,
    time_limit_seconds: float,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "dimensions": dimensions,
        "recursion_step": recursion_step,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "max_recursion_steps": max_recursion_steps,
        "time_limit_seconds": time_limit_seconds,
        "remaining_recursion_steps": max(0, max_recursion_steps - recursion_step),
        "remaining_seconds": max(0.0, round(time_limit_seconds - elapsed_seconds, 3)),
    }


def _render_budget_prompt_block(*, severity: str, payload: dict[str, Any]) -> str:
    heading = (
        "## Runtime Budget Exhausted" if severity == "exhausted" else "## Runtime Budget Warning"
    )
    dimensions = list(payload.get("dimensions") or ())
    reason_lines = []
    if "recursion_steps" in dimensions:
        reason_lines.append("任务链路较长：可能是拆解目标不够细或中间步骤过多")
    if "elapsed_time" in dimensions:
        reason_lines.append(
            "任务处理耗时较久：可能受到外部工具响应慢、视频长时片段解析或环境波动影响"
        )
    if not reason_lines:
        reason_lines.append("运行预算接近阈值")
    lines = [
        heading,
        "",
        f"- severity: {severity}",
        f"- dimensions: {', '.join(dimensions)}",
        (
            "- recursion_steps: "
            f"{payload.get('recursion_step')} / {payload.get('max_recursion_steps')}"
        ),
        (
            f"- elapsed_seconds: {payload.get('elapsed_seconds')} / "
            f"{payload.get('time_limit_seconds')}"
        ),
        ("- 触发提示：" + "；".join(reason_lines)),
    ]
    if severity == "exhausted":
        reasons: list[str] = []
        if "recursion_steps" in dimensions:
            reasons.append("图执行步数已达上限")
        if "elapsed_time" in dimensions:
            reasons.append("运行时间已达上限")
        reason = "、".join(reasons) if reasons else "运行预算已耗尽"
        lines.extend(
            [
                "",
                f"触发原因：{reason}。你必须说明运行预算已耗尽，本次回答不保证覆盖全部信息。",
                "不要再调用任何工具；即使你认为还需要检索、读取、检查或核实，也必须停止工具探索。",
                "基于已有信息回答用户问题。优先输出已经获得的可验证事实；"
                "对未完成核实的部分标注“暂未核实”。",
                "",
                "必须引导用户继续提问，并根据当前问题给出一个自然的继续路径（示例之一）：",
                "1) 如果问题过于宽泛，请改为先问第一个子问题。",
                "2) 如果问题涉及多个步骤，请按“步骤1/步骤2…”分次追问。",
                "3) 如果结果存在不确定性，请让我专注核实某个具体对象或时间点。",
                "",
                "回复末尾可以直接加：“你也可以继续问我：‘把任务拆成第一步：…’。”",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "你已接近运行预算阈值。",
                "请根据剩余预算评估未完成任务，优先核实关键缺口；无法完成的部分应明确说明。",
                "根据当前问题和已完成程度，给出自然的下一步引导；"
                "引导方向可以是继续核实未完成部分、深入重点信息，或推进下一步。",
            ]
        )
    return "\n".join(lines)


class _HistoricalCitationCanonicalizationMiddleware(AgentMiddleware):
    """Canonicalize historical citations in model context."""

    def __init__(
        self,
        state: _RunState,
        message_citation_map: HistoricalMessageCitationMap | None = None,
    ) -> None:
        self._state = state
        self._message_citation_map = message_citation_map or HistoricalMessageCitationMap()

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        return handler(self._inject_context(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._inject_context(request))

    def _inject_context(self, request: Any) -> Any:
        return self._canonicalize_messages(request)

    def _canonicalize_messages(self, request: Any) -> Any:
        messages = getattr(request, "messages", None)
        if not isinstance(messages, list):
            return request
        graph_state = getattr(request, "state", None)
        history_messages = (
            graph_state["messages"]
            if isinstance(graph_state, dict) and "messages" in graph_state
            else messages
        )
        _merge_checkpoint_historical_claims(self._state, history_messages)
        canonicalized = _canonicalize_historical_message_citations(
            messages,
            self._message_citation_map,
            self._state.historical_claims,
        )
        canonicalized = _canonicalize_historical_tool_claim_citations(
            canonicalized,
            self._state.historical_claims,
            self._state.historical_claim_alias_refs,
            call_id_aliases=self._state.tool_call_id_aliases,
        )
        if canonicalized == messages:
            return request
        return request.override(messages=canonicalized)


class _CountingGroundingEnforcerMiddleware(AgentMiddleware):
    """Force visual_ground for static visible-object counting questions.

    Injects a system-level instruction before every model call while the run is
    a counting question and visual_ground has not yet returned a result. Unlike
    appending a HumanMessage (which deepagents' reducer may drop), this appends
    to the system prompt, which the model always sees. Stops injecting once a
    visual_ground ToolMessage is present, capped to avoid unbounded loops.
    """

    _MAX_INJECTIONS = 10
    _INSTRUCTION = (
        "系统要求：这是一个单帧静态可见物体计数问题。"
        "在你的工具调用链中，必须包含一次 `visual_ground` 调用：对选定的最终状态帧"
        "或最相关视频证据执行精确计数，并以 `visual_ground` 返回的检测数量作为"
        "最终可见数量结论。仅调用视频核验工具或引用检索文本数数是不够的；"
        "如果尚未调用 `visual_ground`，禁止给出最终计数答案——下一步必须调用 "
        "`visual_ground`。"
    )

    def __init__(self, op: Any, *, state: _RunState | None = None) -> None:
        self._state = state
        self._user_query = str(getattr(op, "input", "") or "")
        self._injected = 0

    def _is_counting_question(self) -> bool:
        return not (self._state and self._state.budget_finalizing) and (
            _is_static_visible_count_question(self._user_query)
        )

    def _visual_ground_used(self, messages: list[Any]) -> bool:
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and str(getattr(message, "name", "") or "") == "visual_ground"
            ):
                return True
        return False

    def _maybe_inject(self, messages: list[Any]) -> bool:
        """Return True if we injected (caller should not call the model)."""
        return False

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if not self._is_counting_question() or self._injected >= self._MAX_INJECTIONS:
            return handler(request)
        state_messages = self._state_messages_from_request(request)
        if self._visual_ground_used(state_messages):
            return handler(request)
        self._injected += 1
        return handler(self._append_instruction(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if not self._is_counting_question() or self._injected >= self._MAX_INJECTIONS:
            return await handler(request)
        state_messages = self._state_messages_from_request(request)
        if self._visual_ground_used(state_messages):
            return await handler(request)
        self._injected += 1
        return await handler(self._append_instruction(request))

    def _append_instruction(self, request: Any) -> Any:
        system_message = request.system_message
        if system_message is None:
            return request.override(
                system_message=SystemMessage(
                    content_blocks=[{"type": "text", "text": self._INSTRUCTION}]
                )
            )
        new_blocks = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{self._INSTRUCTION}"},
        ]
        return request.override(system_message=SystemMessage(content_blocks=new_blocks))

    @staticmethod
    def _state_messages_from_request(request: Any) -> list[Any]:
        messages = getattr(request, "messages", None)
        if isinstance(messages, list):
            return messages
        return []


class SkillFlowSimulationError(RuntimeError):
    """Raised when a skill-flow gold tool simulation cannot continue."""


class _GoldToolSimulationMiddleware(AgentMiddleware):
    """Replace real tool execution with gold skill-flow tool results."""

    def __init__(self, simulator: GoldToolSimulator) -> None:
        self._simulator = simulator

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        if self._should_passthrough(request):
            return handler(request)
        return self._simulate(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        if self._should_passthrough(request):
            return await handler(request)
        return self._simulate(request)

    def _should_passthrough(self, request: Any) -> bool:
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = str(tool_call.get("name") or "")
        arguments = _simulated_tool_arguments(tool_call)
        return self._simulator.should_passthrough(tool_name, arguments)

    def _simulate(self, request: Any) -> ToolMessage:
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = str(tool_call.get("name") or "")
        tool_call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
        arguments = _simulated_tool_arguments(tool_call)
        result = self._simulator.call(tool_name, arguments)
        if self._simulator.failed:
            raise SkillFlowSimulationError(json.dumps(self._simulator.failure, ensure_ascii=False))
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False),
            tool_call_id=tool_call_id,
            name=tool_name,
        )


def _simulated_tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    value = tool_call.get("args")
    if value is None:
        value = tool_call.get("arguments")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        payload = _parse_json_object(value)
        if payload is not None:
            return payload
    return {}


class _RunToolResultProjectionMiddleware(AgentMiddleware):
    """Project tool results per run while keeping full payloads off the model context."""

    def __init__(self, state: _RunState) -> None:
        self._state = state

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        result = handler(request)
        return self._project_result(request, result)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        result = await handler(request)
        return self._project_result(request, result)

    def _project_result(self, request: Any, result: ToolMessage | Any) -> ToolMessage | Any:
        if isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages")
            if isinstance(messages, (list, tuple)):
                return replace(
                    result,
                    update={
                        **result.update,
                        "messages": [self._project_result(request, item) for item in messages],
                    },
                )
        if not isinstance(result, ToolMessage):
            return result
        tool_name = str((getattr(request, "tool_call", {}) or {}).get("name") or "")
        projected = result
        if tool_name in _INSPECT_TOOL_NAMES:
            projected = _project_inspect_tool_message_for_agent(self._state, tool_name, result)
        elif tool_name in _SEARCH_TOOL_NAMES:
            projected = _project_search_tool_message_for_agent(tool_name, result)
        elif tool_name == "read_file":
            projected = _project_read_file_tool_message_for_agent(result)
        return _append_budget_warning_to_tool_message(self._state, projected)


@dataclass
class _InspectorInvocation:
    task_call_id: str
    inspection_task: InspectionTask
    inspection_calls: list[InspectionCallFact] = field(default_factory=list)
    report_count: int = 0
    report_payload: dict[str, Any] | None = None


class _EvidenceResearchMiddleware(AgentMiddleware):
    """Enforce Oracle research transitions from completed run facts.

    The skill describes the research workflow, while this middleware derives
    its preconditions from successful plan, Scout, and Inspector handoffs.
    It deliberately constrains only ``task`` delegation in the
    an ``oracle_collection`` profile; it is not a global recursion or tool-call
    budget.
    """

    def __init__(
        self,
        state: _RunState,
        *,
        required_skill_name: str = "",
        required_skill_path: str = "",
    ) -> None:
        self._state = state
        self._required_skill_name = required_skill_name
        self._required_skill_path = required_skill_path
        self._skill_bootstrap_requested = False
        self._exact_inspection_lock = asyncio.Lock()
        self._scout_task_lock = asyncio.Lock()
        self._active_scout_page_count: int | None = None
        self._active_scout_report_count: int | None = None
        self._active_scout_page_size: int | None = None
        self._active_scout_page_segment_ids: list[str] | None = None
        self._active_scout_page_payload: dict[str, Any] | None = None
        self._active_scout_report_payload: dict[str, Any] | None = None
        self._active_subagent_role: str | None = None
        self._inspector_invocation: ContextVar[_InspectorInvocation | None] = ContextVar(
            f"evidence_oracle_inspector_invocation_{id(self)}",
            default=None,
        )

    @hook_config(can_jump_to=["tools"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Guarantee the configured workflow skill enters the agent context once.

        The research workflow itself stays in the staged skill.  This harness
        only turns an otherwise silent first model turn into the standard
        DeepAgents ``read_file`` bootstrap, derived from the active profile
        configuration rather than an Oracle-specific prompt embedded here.
        """

        if self._current_subagent_role() is not None:
            return None

        messages = state.get("messages", [])
        last_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if (
            not self._required_skill_name
            or not self._required_skill_path
            or self._required_skill_name in self._state.read_skills
            or self._skill_bootstrap_requested
        ):
            if last_message is None or last_message.tool_calls:
                return None
            if self._has_completed_tool("evidence_package"):
                return None
            reason_code, next_action = self._oracle_final_gate_recovery()
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "evidence_oracle_gate_check",
                                "args": {
                                    "reason_code": reason_code,
                                    "next_action": next_action,
                                },
                                "id": f"oracle-final-gate-{uuid4().hex}",
                                "type": "tool_call",
                            }
                        ],
                    )
                ],
                "jump_to": "tools",
            }
        if last_message is None or last_message.tool_calls:
            return None
        self._skill_bootstrap_requested = True
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": self._required_skill_path},
                            "id": f"skill-bootstrap-{uuid4().hex}",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "jump_to": "tools",
        }

    def _oracle_final_gate_recovery(self) -> tuple[str, str]:
        if self._completed_evidence_plan() is None:
            return (
                "oracle_evidence_plan_required",
                "先调用 evidence_plan 建立不可变覆盖合同，再继续候选采集。",
            )
        pending_cursor = self._pending_catalog_shard_cursor()
        if pending_cursor is not None:
            return (
                "oracle_catalog_shard_pending",
                "继续委派 evidence-scout 完成并提交当前目录页的 ScoutReport："
                f"cursor={pending_cursor}。",
            )
        event_map = self._completed_evidence_event_map()
        if event_map is None:
            available_frontier_ids = sorted(
                self._retained_frontier_ids(self._completed_scout_reports())
            )
            return (
                "oracle_event_map_required",
                "调用 evidence_event_map 对以下本 run 真实 frontier_ids 分组："
                f"{json.dumps(available_frontier_ids, ensure_ascii=False)}。",
            )
        events = event_map.get("events", []) if isinstance(event_map, dict) else []
        reported_event_ids = {
            str(report.get("event_id"))
            for report in self._completed_inspection_reports()
            if report.get("event_id")
        }
        missing_event_ids = [
            str(event.get("event_id"))
            for event in events
            if isinstance(event, dict)
            and event.get("event_id")
            and str(event.get("event_id")) not in reported_event_ids
        ]
        if missing_event_ids:
            return (
                "oracle_exhaustive_events_uninspected",
                "继续委派 evidence-inspector 核验尚无规范报告的事件："
                + ", ".join(missing_event_ids),
            )
        if self._completed_research_progress() is None or (
            self._has_inspector_result_after_latest_progress()
        ):
            return (
                "oracle_research_progress_required",
                "审阅全部 canonical Inspector handoff，调用 evidence_progress 更新每个 slot "
                "及事件状态。",
            )
        latest_progress = self._completed_research_progress()
        unresolved_event_ids = self._unresolved_event_ids(latest_progress)
        if unresolved_event_ids and not self._has_lineage_reinspection_report(unresolved_event_ids):
            reflection = self._completed_research_reflection()
            if not self._reflection_matches_progress(reflection, latest_progress):
                return (
                    "oracle_research_reflection_required",
                    "仍有 unresolved event；先基于最新 progress 调用 evidence_reflection，"
                    "明确当前假设、反例与下一轮 challenge。",
                )
            return (
                "oracle_research_challenge_required",
                self._lineage_reinspection_instruction(unresolved_event_ids),
            )
        return (
            "oracle_candidate_package_required",
            "本 run 尚无成功 CandidateEvidencePackage；根据最新 progress 重试 "
            "evidence_package。若仍有缺口，使用 research_incomplete 并保留真实 scope locator。",
        )

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        request, event_map_rejection = self._prepare_event_map_request(request)
        if event_map_rejection is not None:
            return event_map_rejection
        request = self._inject_immutable_research_plan(request)
        request = self._inject_latest_research_progress(request)
        request = self._inject_catalog_mapping_scout_task(request)
        blocked = self._blocked_result(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        request, inspection_report_rejection = self._prepare_inspection_report_request(request)
        if inspection_report_rejection is not None:
            return inspection_report_rejection
        request, decision_rejection = self._prepare_scout_report_request(request)
        if decision_rejection is not None:
            return decision_rejection
        request, event_map_rejection = self._prepare_event_map_request(request)
        if event_map_rejection is not None:
            return event_map_rejection
        request = self._inject_immutable_research_plan(request)
        request = self._inject_latest_research_progress(request)
        request = self._inject_catalog_mapping_scout_task(request)
        request = self._inject_active_scout_page_size(request)
        tool_call = self._tool_call(request)
        blocked = self._blocked_result(request)
        if blocked is not None:
            return blocked
        task_role = self._task_role(tool_call)
        if task_role == "evidence-scout":
            async with self._scout_task_lock:
                self._active_scout_page_count = 0
                self._active_scout_report_count = 0
                self._active_scout_page_size = self._scout_catalog_page_size(tool_call)
                self._active_scout_page_segment_ids = None
                self._active_scout_page_payload = None
                self._active_scout_report_payload = None
                self._active_subagent_role = task_role
                try:
                    result = await handler(request)
                    if (
                        self._active_scout_page_count
                        and self._requires_exhaustive_event_coverage()
                        and self._active_scout_report_count != 1
                    ):
                        return self._rejection(
                            request,
                            phase="content_mapping",
                            reason_code="oracle_scout_report_required",
                            next_action=(
                                "Scout 完成当前目录页后必须且只能调用一次 evidence_scout_report，"
                                "持久化 candidates、material_exclusions 与 cursor 覆盖事实，"
                                "再返回主 Agent。"
                            ),
                        )
                    return self._append_canonical_scout_handoff(result)
                finally:
                    self._active_scout_page_count = None
                    self._active_scout_report_count = None
                    self._active_scout_page_size = None
                    self._active_scout_page_segment_ids = None
                    self._active_scout_page_payload = None
                    self._active_scout_report_payload = None
                    self._active_subagent_role = None
        if task_role == "evidence-inspector":
            request = self._inject_canonical_inspection_task(request)
            tool_call = self._tool_call(request)
            inspection_task = self._inspection_task_from_delegation(tool_call)
            if inspection_task is None:
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_inspection_task_required",
                    next_action="使用有效的单个 InspectionTask JSON 后重试。",
                )
            invocation = _InspectorInvocation(
                task_call_id=str(tool_call.get("id") or tool_call.get("call_id") or ""),
                inspection_task=inspection_task,
            )
            token = self._inspector_invocation.set(invocation)
            try:
                result = await handler(request)
                if not invocation.inspection_calls:
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_inspection_tool_required",
                        next_action=(
                            "Inspector 尚未调用核验工具；必须先用 video_inspect_2_0、"
                            "text_inspect、"
                            "image_inspect 或 visual_ground 检查该 InspectionTask 的"
                            "时间窗或 locator。"
                            "只有工具返回结构化不可用错误时，才能报告 coverage gap。"
                        ),
                    )
                if invocation.report_count != 1 or invocation.report_payload is None:
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_inspection_report_required",
                        next_action=(
                            "Inspector 完成局部核验后必须且只能调用一次 "
                            "evidence_inspection_report；逐项裁定每个 verification target，"
                            "再返回主 Agent。"
                        ),
                    )
                return self._append_canonical_inspection_handoff(result, invocation.report_payload)
            finally:
                self._inspector_invocation.reset(token)
        request = self._bind_video_inspection_to_event(request)
        tool_call = self._tool_call(request)
        page_rejection = self._scout_page_rejection(request)
        if page_rejection is not None:
            return page_rejection
        if self._is_scout_page_request(request):
            result = await handler(request)
            if not _tool_result_has_error(_message_tool_result(result)):
                self._active_scout_page_segment_ids = self._catalog_page_segment_ids(result)
                self._active_scout_page_payload = self._catalog_page_payload(result)
                self._active_scout_page_count = (self._active_scout_page_count or 0) + 1
            return result
        if str(tool_call.get("name") or "") == "evidence_scout_report":
            if (
                self._active_subagent_role == "evidence-scout"
                and (self._active_scout_report_count or 0) >= 1
            ):
                return self._rejection(
                    request,
                    phase="content_mapping",
                    reason_code="oracle_scout_report_already_submitted",
                    next_action=(
                        "此页已经完成结构化 ScoutReport 交接；不要再次提交 report，"
                        "立即返回主 Agent 处理下一步研究决策。"
                    ),
                )
            result = await handler(request)
            if not _tool_result_has_error(_message_tool_result(result)):
                self._active_scout_report_count = (self._active_scout_report_count or 0) + 1
                payload = parse_tool_payload(_message_tool_result(result))
                report = payload.get("scout_report") if isinstance(payload, dict) else None
                if isinstance(report, dict):
                    self._active_scout_report_payload = report
            return result
        if str(tool_call.get("name") or "") == "evidence_inspection_report":
            invocation = self._inspector_invocation.get()
            if invocation is None:
                return self._rejection(
                    request,
                    phase="role_boundary",
                    reason_code="oracle_inspection_report_outside_task",
                    next_action="该工具只能由 evidence-inspector 在 InspectionTask 内调用。",
                )
            if invocation.report_count >= 1:
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_inspection_report_already_submitted",
                    next_action="该 InspectionTask 已提交结构化报告；立即返回主 Agent。",
                )
            result = await handler(request)
            if not _tool_result_has_error(_message_tool_result(result)):
                invocation.report_count += 1
                payload = parse_tool_payload(_message_tool_result(result))
                report = payload.get("inspection_report") if isinstance(payload, dict) else None
                if isinstance(report, dict):
                    invocation.report_payload = report
            return result
        if str(tool_call.get("name") or "") in _INSPECT_TOOL_NAMES:
            invocation = self._inspector_invocation.get()
            arguments = tool_call.get("args", tool_call.get("arguments", {}))
            if isinstance(arguments, str):
                arguments = _parse_json_object(arguments) or {}
            if invocation is not None and any(
                call.success
                and call.tool_name == str(tool_call.get("name") or "")
                and call.arguments == arguments
                for call in invocation.inspection_calls
            ):
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_duplicate_inspection_call",
                    next_action=(
                        "该 Inspector 已使用完全相同的工具参数完成过一次调用。"
                        "复用已有结果并调用 evidence_inspection_report；"
                        "若参数有误，修正 locator、时间窗或问题后再调用。"
                    ),
                )
            if invocation is not None:
                successful_tool_names = {
                    call.tool_name for call in invocation.inspection_calls if call.success
                }
                tool_name = str(tool_call.get("name") or "")
                allowed_visual_followup = (
                    bool(successful_tool_names)
                    and successful_tool_names <= VIDEO_INSPECT_TOOL_NAMES
                    and tool_name == "visual_ground"
                )
                if successful_tool_names and not allowed_visual_followup:
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_inspection_event_already_observed",
                        next_action=(
                            "该 Inspector 已完成此事件允许的核验调用；复用已有结果并调用 "
                            "evidence_inspection_report。视频事件仅允许一次完整的"
                            "视频核验调用，"
                            "必要时可再调用一次 visual_ground 补充单帧定位。"
                        ),
                    )
            # An exact inspection is an evidence-producing boundary.  Keep its
            # calls ordered so the next selection sees a complete prior result.
            async with self._exact_inspection_lock:
                result = await handler(request)
            if invocation is not None:
                invocation.inspection_calls.append(self._inspection_call_fact(tool_call, result))
            return result
        return await handler(request)

    def _bind_video_inspection_to_event(self, request: Any) -> Any:
        """Use one canonical event window for an Inspector's video observation."""

        tool_call = self._tool_call(request)
        invocation = self._inspector_invocation.get()
        if str(tool_call.get("name") or "") not in VIDEO_INSPECT_TOOL_NAMES or invocation is None:
            return request
        locators = invocation.inspection_task.locators
        identities = {
            (str(locator.get("library_id") or ""), str(locator.get("asset_id") or ""))
            for locator in locators
            if isinstance(locator, dict) and locator.get("library_id") and locator.get("asset_id")
        }
        if len(identities) != 1:
            return request
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return request
        library_id, asset_id = next(iter(identities))
        canonical_arguments = dict(arguments)
        canonical_arguments.pop("evidence_id", None)
        canonical_arguments.pop("start_seconds", None)
        canonical_arguments.pop("end_seconds", None)
        canonical_arguments.update(
            {
                "library_id": library_id,
                "asset_id": asset_id,
                "time_range": dict(invocation.inspection_task.time_range or {}),
            }
        )
        return self._replace_tool_call_arguments(request, tool_call, canonical_arguments)

    def _prepare_inspection_report_request(self, request: Any) -> tuple[Any, ToolMessage | None]:
        """Bind Inspector judgments to its task and actual tool-call facts."""

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") != "evidence_inspection_report":
            return request, None
        invocation = self._inspector_invocation.get()
        if invocation is None:
            return request, None
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return request, None
        try:
            findings = [
                InspectionFindingDecision.model_validate(item)
                for item in arguments.get("findings", [])
            ]
            report = build_inspection_report(
                task_call_id=invocation.task_call_id,
                inspection_task=invocation.inspection_task,
                inspection_calls=invocation.inspection_calls,
                status=arguments.get("status"),
                findings=findings,
                coverage_gaps=list(arguments.get("coverage_gaps", [])),
            )
        except (TypeError, ValueError) as exc:
            return request, self._rejection(
                request,
                phase="exact_verification",
                reason_code="oracle_inspection_report_invalid",
                next_action=(
                    "修正 Inspector report 后重试；每个 verification target 必须恰好裁定一次，"
                    f"且报告必须绑定已经执行的 inspect 调用。全部已发现问题：{exc}"
                ),
            )
        return (
            self._replace_tool_call_arguments(
                request,
                tool_call,
                {"inspection_report": report.model_dump(mode="json")},
            ),
            None,
        )

    def _inject_canonical_inspection_task(self, request: Any) -> Any:
        """Attach Event Map locators so Inspector never guesses stable asset identity."""

        tool_call = self._tool_call(request)
        inspection_task = self._inspection_task_from_delegation(tool_call)
        event_map = self._completed_evidence_event_map()
        if inspection_task is None or not isinstance(event_map, dict):
            return request
        event = next(
            (
                item
                for item in event_map.get("events", [])
                if isinstance(item, dict) and item.get("event_id") == inspection_task.event_id
            ),
            None,
        )
        if event is None:
            return request
        frontier_ids = set(inspection_task.frontier_ids)
        locators = [
            locator
            for locator in event.get("locators", [])
            if isinstance(locator, dict) and locator.get("segment_id") in frontier_ids
        ]
        canonical_task = inspection_task.model_copy(update={"locators": locators})
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return request
        updated_arguments = dict(arguments)
        updated_arguments["description"] = json.dumps(
            canonical_task.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._replace_tool_call_arguments(request, tool_call, updated_arguments)

    @staticmethod
    def _inspection_call_fact(
        tool_call: dict[str, Any], result: ToolMessage | Any
    ) -> InspectionCallFact:
        content = _message_tool_result(result)
        payload = parse_tool_payload(content)
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        return InspectionCallFact(
            tool_call_id=str(tool_call.get("id") or tool_call.get("call_id") or ""),
            tool_name=str(tool_call.get("name") or ""),
            arguments=arguments if isinstance(arguments, dict) else {},
            success=not _tool_result_has_error(content),
            claim_refs=_collect_claim_refs(payload),
            error_code=str(error.get("code")) if error.get("code") else None,
        )

    @staticmethod
    def _append_canonical_inspection_handoff(result: Any, report: dict[str, Any]) -> Any:
        if not isinstance(result, ToolMessage):
            return result
        content = f"{result.content}\n\nCANONICAL_INSPECTION_HANDOFF=" + json.dumps(
            report, ensure_ascii=False, separators=(",", ":")
        )
        return result.model_copy(update={"content": content})

    def _current_subagent_role(self) -> str | None:
        if self._inspector_invocation.get() is not None:
            return "evidence-inspector"
        return self._active_subagent_role

    def _scout_page_rejection(self, request: Any) -> ToolMessage | None:
        """Keep one Scout delegation inside one bounded catalog shard."""

        if self._active_scout_page_count is None:
            return None
        if not self._is_scout_page_request(request):
            return None
        tool_call = self._tool_call(request)
        expected_tool = self._known_catalog_tool_name()
        actual_tool = str(tool_call.get("name") or "")
        if expected_tool is not None and actual_tool != expected_tool:
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_catalog_tool_mismatch",
                next_action=(
                    f"当前 catalog 由 {expected_tool} 建立；使用同一工具读取指定 page，"
                    "不要跨视频/文档模态试探。"
                ),
            )
        arguments = tool_call.get("args", {})
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        page_size = arguments.get("page_size") if isinstance(arguments, dict) else None
        if page_size is not None and int(page_size) > 40:
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_page_size_limit",
                next_action=(
                    "Oracle Scout 必须使用有界目录 shard；将 page_size=40（或省略以使用默认值），"
                    "审阅当前 page 后返回紧凑 ScoutReport。"
                ),
            )
        if self._active_scout_page_count >= 1:
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_shard_page_limit",
                next_action=(
                    "当前 Scout 已读取一个有界目录 shard；返回紧凑 ScoutReport，"
                    "由主 Agent 根据 coverage 决定是否委派下一 shard。"
                ),
            )
        return None

    def _append_canonical_scout_handoff(self, result: Any) -> Any:
        """Expose accepted frontier facts across the subagent context boundary."""

        report = self._active_scout_report_payload
        if report is None or not isinstance(result, ToolMessage):
            return result
        candidate_ids = set(report.get("candidate_segment_ids", []))
        uncertain_ids = set(report.get("uncertain_segment_ids", []))
        locators = report.get("locators", [])
        candidate_frontiers = [
            item
            for item in locators
            if isinstance(item, dict) and item.get("frontier_id") in candidate_ids
        ]
        uncertain_frontiers = [
            item
            for item in locators
            if isinstance(item, dict) and item.get("frontier_id") in uncertain_ids
        ]
        handoff = {
            "catalog_id": report.get("catalog_id"),
            "shard_cursor": report.get("shard_cursor"),
            "next_cursor": report.get("next_cursor"),
            "candidate_frontiers": candidate_frontiers,
            "uncertain_frontiers": uncertain_frontiers,
            "material_exclusions": report.get("material_exclusions", []),
        }
        content = f"{result.content}\n\nCANONICAL_SCOUT_HANDOFF=" + json.dumps(
            handoff, ensure_ascii=False, separators=(",", ":")
        )
        return result.model_copy(update={"content": content})

    def _is_scout_page_request(self, request: Any) -> bool:
        """Return whether a tool call consumes a successful Scout catalog page."""

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") not in {
            "list_video_segments",
            "list_doc_segments",
        }:
            return False
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        return isinstance(arguments, dict) and str(arguments.get("mode") or "") == "page"

    def _blocked_result(self, request: Any) -> ToolMessage | None:
        tool_call = self._tool_call(request)
        tool_name = str(tool_call.get("name") or "")
        if tool_name == "evidence_progress" and self._completed_evidence_plan() is None:
            return self._rejection(
                request,
                phase="research_progress",
                reason_code="oracle_progress_requires_plan",
                next_action=(
                    "先完成 evidence_plan；进度中的计划和核验记录必须来自本 run 的实际结果。"
                ),
            )
        if self._current_subagent_role() is None and tool_name not in {
            "read_file",
            "write_todos",
            "task",
            "evidence_plan",
            "evidence_event_map",
            "evidence_progress",
            "evidence_reflection",
            "evidence_exclusion_review",
            "evidence_package",
        }:
            return self._rejection(
                request,
                phase="role_boundary",
                reason_code="oracle_main_role_tool_forbidden",
                next_action=(
                    "主 Agent 只能编排；将目录、检索与候选定位委派给 evidence-scout，"
                    "将精确核验委派给 evidence-inspector。"
                ),
            )
        if self._is_scout_select_request(request):
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_select_disabled",
                next_action=(
                    "Evidence Oracle 的 Scout 暂不使用 select 初筛；先读取当前目录 page，"
                    "审阅该 shard 的 caption/summary、ASR 与时间范围，再返回紧凑 ScoutReport。"
                ),
            )
        if self._requires_skill_bootstrap(tool_call):
            return self._rejection(
                request,
                phase="skill_bootstrap",
                reason_code="oracle_skill_not_loaded",
                next_action="先加载当前运行已配置的 workflow skill，再继续。",
            )
        if tool_name == "evidence_package":
            return self._package_submission_rejection(request)
        if tool_name == "evidence_reflection" and self._completed_research_progress() is None:
            return self._rejection(
                request,
                phase="research_reflection",
                reason_code="oracle_research_progress_required",
                next_action=(
                    "主 Agent 必须先调用 evidence_progress，记录当前 ResearchPlan 的事实进度、"
                    "缺口与事件状态，再反思假设、反例和下一轮挑战。"
                ),
            )
        if tool_name == "evidence_event_map" and self._pending_catalog_shard_cursor() is not None:
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_catalog_shard_pending",
                next_action=(
                    "目录仍有未完成 shard；先完成完整 Catalog Mapping round，"
                    "再由主 Agent 基于全局 ledger 建立事件地图。"
                ),
            )
        if tool_name == "evidence_event_map" and self._requires_exhaustive_event_coverage():
            scout_reports = self._completed_scout_reports()
            if not scout_reports:
                return self._rejection(
                    request,
                    phase="event_modeling",
                    reason_code="oracle_scout_reports_required",
                    next_action=(
                        "这是穷尽覆盖问题。每个 Scout catalog page 必须先通过 "
                        "evidence_scout_report 持久化候选与 material exclusions，"
                        "主 Agent 才能建立事件地图。"
                    ),
                )
            material_exclusion_ids = self._material_exclusion_frontier_ids(scout_reports)
            review = self._completed_exclusion_review()
            if material_exclusion_ids and review is None:
                return self._rejection(
                    request,
                    phase="event_modeling",
                    reason_code="oracle_exclusion_review_required",
                    next_action=(
                        "这是穷尽覆盖问题。主 Agent 必须先调用 evidence_exclusion_review，"
                        "逐项接受或重开 Scout 报告中的 material exclusions；"
                        "重开项必须写明具体 challenge 并进入事件地图。"
                    ),
                )
            reviewed_frontier_ids = self._reviewed_exclusion_frontier_ids(review)
            missing_review_ids = sorted(material_exclusion_ids - reviewed_frontier_ids)
            if missing_review_ids:
                return self._rejection(
                    request,
                    phase="event_modeling",
                    reason_code="oracle_material_exclusions_unreviewed",
                    next_action=(
                        "Scout 报告的 material exclusions 必须逐项进入 "
                        "evidence_exclusion_review："
                        f"{', '.join(missing_review_ids)}。"
                    ),
                )
            reopened_frontier_ids = self._reopened_exclusion_frontier_ids(review)
            if reopened_frontier_ids:
                arguments = tool_call.get("args", tool_call.get("arguments", {}))
                if isinstance(arguments, str):
                    arguments = _parse_json_object(arguments) or {}
                events = arguments.get("events", []) if isinstance(arguments, dict) else []
                mapped_frontier_ids = {
                    str(frontier_id)
                    for event in events
                    if isinstance(event, dict)
                    for frontier_id in event.get("frontier_ids", [])
                }
                missing_frontier_ids = sorted(reopened_frontier_ids - mapped_frontier_ids)
                if missing_frontier_ids:
                    return self._rejection(
                        request,
                        phase="event_modeling",
                        reason_code="oracle_reopened_exclusion_not_event_mapped",
                        next_action=(
                            "已重开的 Scout exclusion 必须先写入 evidence_event_map 的 "
                            f"frontier_ids：{', '.join(missing_frontier_ids)}；"
                            "随后再为它建立局部 Inspector 核验任务。"
                        ),
                    )
            arguments = tool_call.get("args", tool_call.get("arguments", {}))
            if isinstance(arguments, str):
                arguments = _parse_json_object(arguments) or {}
            events = arguments.get("events", []) if isinstance(arguments, dict) else []
            mapped_frontier_ids = {
                str(frontier_id)
                for event in events
                if isinstance(event, dict)
                for frontier_id in event.get("frontier_ids", [])
            }
            retained_frontier_ids = self._retained_frontier_ids(scout_reports)
            missing_frontier_ids = sorted(retained_frontier_ids - mapped_frontier_ids)
            if missing_frontier_ids:
                return self._rejection(
                    request,
                    phase="event_modeling",
                    reason_code="oracle_event_frontiers_unmapped",
                    next_action=(
                        "穷尽覆盖问题的事件地图必须处置每个 candidate/uncertain frontier；"
                        "将以下真实 frontier_ids 分组进 decisions，不得缩写或猜测："
                        f"{', '.join(missing_frontier_ids)}。"
                    ),
                )
        if tool_name == "evidence_event_map" and self._completed_evidence_event_map() is not None:
            return self._rejection(
                request,
                phase="event_modeling",
                next_action="事件地图已完成；使用其 event_id 与 locator 委派 Inspector。",
            )
        if tool_name == "evidence_plan" and self._has_completed_tool("evidence_plan"):
            return self._rejection(
                request,
                phase="coverage_planning",
                next_action=(
                    "Coverage contract 已完成；复用已有 subquestions，"
                    "委派 evidence-scout 完成 Content map 与候选选择。"
                ),
            )
        role = self._task_role(tool_call)
        if role is None:
            return None
        if role == "evidence-scout":
            if not self._has_completed_scope_discovery():
                return None
            if not self._has_completed_tool("evidence_plan"):
                return self._rejection(
                    request,
                    phase="content_mapping",
                    reason_code="oracle_phase_prerequisite_missing",
                    next_action=(
                        "Scope Discovery 已完成；现在必须调用 evidence_plan，再进行内容建图。"
                    ),
                )
            if self._has_completed_content_mapping():
                if self._pending_catalog_shard_cursor() is not None:
                    return None
                if self._has_inspection_coverage_gap():
                    reflection = self._completed_research_reflection()
                    if reflection is None:
                        return self._rejection(
                            request,
                            phase="targeted_research",
                            reason_code="oracle_research_reflection_required",
                            next_action=(
                                "Inspector 已报告 coverage gap；主 Agent 先调用 evidence_progress，"
                                "再调用 evidence_reflection，写明当前假设、反例与下一轮 challenge，"
                                "然后委派绑定 reflection_id 的 targeted Scout。"
                            ),
                        )
                    task = self._targeted_scout_task_from_delegation(tool_call)
                    if task is not None and task.reflection_id == reflection.get("reflection_id"):
                        return None
                    return self._rejection(
                        request,
                        phase="targeted_research",
                        reason_code="oracle_targeted_scout_task_required",
                        next_action=(
                            "完整目录建图已经结束；后续 Scout 必须使用 ScoutTask JSON 指定 "
                            "phase=targeted_research、research_round、已知 prior_events、"
                            "最新 evidence_reflection 的 reflection_id，以及会改变当前结论的 "
                            "challenge。不得再次只转发原问题。"
                        ),
                    )
                return self._rejection(
                    request,
                    phase="content_mapping",
                    next_action=(
                        "Content map 与候选选择已完成；复用已有 locator。"
                        "若 locator 尚未核验，委派 evidence-inspector；"
                        "若已核验，调用 evidence_package。"
                    ),
                )
        if role == "evidence-inspector":
            if not self._has_completed_content_mapping():
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_phase_prerequisite_missing",
                    next_action=(
                        "必须先完成 Content map 与候选选择，Inspector 只能使用其交付的 locator。"
                    ),
                )
            if self._pending_catalog_shard_cursor() is not None:
                return self._rejection(
                    request,
                    phase="content_mapping",
                    reason_code="oracle_catalog_shard_pending",
                    next_action=(
                        "目录尚未完成逐页审阅；先继续委派 evidence-scout 消费下一 page，"
                        "完成全量目录候选筛选后再委派 Inspector。"
                    ),
                )
            if self._completed_evidence_event_map() is None:
                return self._rejection(
                    request,
                    phase="event_modeling",
                    reason_code="oracle_event_map_required",
                    next_action=(
                        "目录已闭合；主 Agent 必须先调用 evidence_event_map，"
                        "将 Scout locator 合并为带 event_id、verification_targets 的语义事件；"
                        "视频事件还必须带 time_range，再委派 Inspector。"
                    ),
                )
            inspection_task = self._inspection_task_from_delegation(tool_call)
            if inspection_task is None:
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_inspection_task_required",
                    next_action=(
                        "主 Agent 必须先把候选 locator 合并为事件，再将 task.description "
                        "写成单个 InspectionTask JSON：event_id、requirement_ids、frontier_ids、"
                        "verification_targets、decision_use；仅视频事件需要 time_range。"
                        "Inspector 只核验这些 "
                        "局部事实，不判断最终答案或总次数。"
                    ),
                )
            event_map = self._completed_evidence_event_map()
            if not self._inspection_task_matches_event_map(inspection_task, event_map):
                events = event_map.get("events", []) if isinstance(event_map, dict) else []
                event_hints = [
                    {
                        "event_id": event.get("event_id"),
                        "frontier_ids": event.get("frontier_ids", []),
                        "time_range": event.get("time_range"),
                    }
                    for event in events
                    if isinstance(event, dict)
                ]
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_inspection_event_mismatch",
                    next_action=(
                        "InspectionTask 必须引用已完成 Event Map 中的 event_id，且 frontier_ids、"
                        "requirement_ids 与时间窗不得越出该事件。可用事件："
                        f"{json.dumps(event_hints, ensure_ascii=False)}"
                    ),
                )
            inspection_reports = self._completed_inspection_reports()
            reports_for_event = [
                report
                for report in inspection_reports
                if str(report.get("event_id") or "") == inspection_task.event_id
            ]
            reported_event_ids = {
                str(report.get("event_id"))
                for report in inspection_reports
                if report.get("event_id")
            }
            if reports_for_event:
                remaining_event_ids = [
                    str(event.get("event_id"))
                    for event in (event_map or {}).get("events", [])
                    if isinstance(event, dict)
                    and event.get("event_id")
                    and str(event.get("event_id")) not in reported_event_ids
                ]
                latest_report = reports_for_event[-1]
                latest_task_call_id = str(latest_report.get("task_call_id") or "")
                prior_refs = set(inspection_task.prior_report_task_call_ids)
                if str(latest_report.get("status") or "") != "incomplete":
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_inspection_event_already_reported",
                        next_action=(
                            "该 event 的最新规范 Inspector 报告已经 complete，不得因任务措辞"
                            "变化重复委派。继续核验未报告事件：" + ", ".join(remaining_event_ids)
                        ),
                    )
                if not latest_task_call_id or latest_task_call_id not in prior_refs:
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_reinspection_latest_report_required",
                        next_action=(
                            "复查 incomplete event 时，InspectionTask 必须在 "
                            "prior_report_task_call_ids 中引用最新报告 "
                            f"{latest_task_call_id}，并说明新的 challenge。"
                        ),
                    )
                previous_targets = {
                    str(target).strip().casefold()
                    for report in reports_for_event
                    for target in report.get("verification_targets", [])
                    if str(target).strip()
                }
                current_targets = {
                    target.strip().casefold()
                    for target in inspection_task.verification_targets
                    if target.strip()
                }
                if current_targets.issubset(previous_targets):
                    return self._rejection(
                        request,
                        phase="exact_verification",
                        reason_code="oracle_reinspection_new_target_required",
                        next_action=(
                            "复查不能只改写任务措辞或重复原目标；请根据 prior report 的 "
                            "coverage_gaps 提出至少一个新的、可局部核验的 verification target。"
                        ),
                    )
            elif inspection_task.prior_report_task_call_ids:
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_reinspection_prior_report_unknown",
                    next_action=(
                        "当前 event 没有可引用的 prior Inspector report；请提交初次核验任务。"
                    ),
                )
            if self._has_completed_task_with_same_arguments(tool_call):
                return self._rejection(
                    request,
                    phase="exact_verification",
                    next_action=(
                        "该 locator 已完成精确核验；复用已有 inspect 结果，"
                        "继续核验其他 locator 或调用 evidence_package。"
                    ),
                )
        return None

    def _is_scout_select_request(self, request: Any) -> bool:
        """Return whether the active Oracle Scout is attempting lexical selection."""

        if self._active_scout_page_count is None:
            return False
        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") not in {
            "list_video_segments",
            "list_doc_segments",
        }:
            return False
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        return isinstance(arguments, dict) and str(arguments.get("mode") or "") == "select"

    def _requires_skill_bootstrap(self, tool_call: dict[str, Any]) -> bool:
        if not self._required_skill_name or self._required_skill_name in self._state.read_skills:
            return False
        tool_name = str(tool_call.get("name") or "")
        if tool_name == "read_file":
            arguments = tool_call.get("args", tool_call.get("arguments", {}))
            if isinstance(arguments, str):
                arguments = _parse_json_object(arguments) or {}
            if isinstance(arguments, dict):
                path = str(arguments.get("file_path") or arguments.get("path") or "")
                return path != self._required_skill_path
            return True
        return tool_name in {
            "task",
            "evidence_plan",
            "evidence_event_map",
            "evidence_progress",
            "evidence_reflection",
            "evidence_exclusion_review",
            "evidence_package",
        }

    def _package_submission_rejection(self, request: Any) -> ToolMessage | None:
        """Require the final package to retain the run's original EvidencePlan."""

        expected_plan = self._completed_evidence_plan()
        if expected_plan is None:
            return self._rejection(
                request,
                phase="package_submission",
                reason_code="oracle_phase_prerequisite_missing",
                next_action=(
                    "必须先完成 evidence_plan，并以其返回的 immutable ResearchPlan "
                    "构造 research_state 后才能提交 evidence_package。"
                ),
            )
        arguments = self._tool_call(request).get("args", {})
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        research_state = arguments.get("research_state")
        submitted_plan = research_state.get("plan") if isinstance(research_state, dict) else None
        if submitted_plan != expected_plan:
            return self._rejection(
                request,
                phase="package_submission",
                reason_code="oracle_research_plan_mismatch",
                next_action=(
                    "research_state.plan 必须原样复用本 run evidence_plan 返回的 "
                    "ResearchPlan；更新 progress，而不是改写 subquestion 或 coverage 条件。"
                ),
            )
        pending_cursor = self._pending_catalog_shard_cursor()
        if pending_cursor is not None:
            catalog_id = self._known_catalog_id()
            catalog_hint = f"catalog_id={catalog_id}，" if catalog_id is not None else ""
            return self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_catalog_shard_pending",
                next_action=(
                    "目录仍有未完成 shard（"
                    f"{catalog_hint}cursor={pending_cursor}）；"
                    "委派 evidence-scout 读取该单个 page，"
                    "由其返回紧凑 ScoutReport 后再决定 Inspector 或下一 shard。"
                ),
            )
        if self._requires_exhaustive_event_coverage():
            event_map = self._completed_evidence_event_map()
            events = event_map.get("events", []) if isinstance(event_map, dict) else []
            expected_event_ids = {
                str(event.get("event_id"))
                for event in events
                if isinstance(event, dict) and event.get("event_id")
            }
            inspected_event_ids = {
                str(report.get("event_id"))
                for report in self._completed_inspection_reports()
                if report.get("event_id")
            }
            missing_event_ids = sorted(expected_event_ids - inspected_event_ids)
            if missing_event_ids:
                return self._rejection(
                    request,
                    phase="exact_verification",
                    reason_code="oracle_exhaustive_events_uninspected",
                    next_action=(
                        "这是穷尽覆盖问题；Event Map 中每个事件都必须完成一次结构化 Inspector "
                        "核验后才能打包。继续核验：" + ", ".join(missing_event_ids)
                    ),
                )
        latest_progress = self._completed_research_progress()
        if latest_progress is None:
            return self._rejection(
                request,
                phase="package_submission",
                reason_code="oracle_research_progress_required",
                next_action=(
                    "主 Agent 必须先调用 evidence_progress，记录当前 ResearchPlan 每个 slot "
                    "的已完成检查、缺口和下一步，再提交 evidence_package。"
                ),
            )
        if self._has_inspector_result_after_latest_progress():
            return self._rejection(
                request,
                phase="package_submission",
                reason_code="oracle_research_progress_stale",
                next_action=(
                    "最近一次 evidence_progress 之后又完成了 Inspector 核验；"
                    "先审阅新增局部事实并更新 progress，再提交 evidence_package。"
                ),
            )
        if latest_progress != research_state:
            return self._rejection(
                request,
                phase="package_submission",
                reason_code="oracle_research_progress_stale",
                next_action=(
                    "evidence_package.research_state 必须与本 run 最新的 evidence_progress "
                    "结果完全一致；如调查有新反馈，先更新 evidence_progress。"
                ),
            )
        unresolved_event_ids = self._unresolved_event_ids(latest_progress)
        if unresolved_event_ids and not self._has_lineage_reinspection_report(unresolved_event_ids):
            reflection = self._completed_research_reflection()
            if not self._reflection_matches_progress(reflection, latest_progress):
                return self._rejection(
                    request,
                    phase="research_reflection",
                    reason_code="oracle_research_reflection_required",
                    next_action=(
                        "仍有 unresolved event。先调用 evidence_reflection，基于最新 progress "
                        "明确当前假设、反例与下一轮 challenge，再决定如何复查。"
                    ),
                )
            return self._rejection(
                request,
                phase="exact_verification",
                reason_code="oracle_research_challenge_required",
                next_action=self._lineage_reinspection_instruction(unresolved_event_ids),
            )
        return None

    @staticmethod
    def _unresolved_event_ids(research_state: dict[str, Any] | None) -> list[str]:
        if not isinstance(research_state, dict):
            return []
        events = research_state.get("events")
        if not isinstance(events, list):
            return []
        return sorted(
            str(event.get("event_id"))
            for event in events
            if isinstance(event, dict)
            and event.get("event_id")
            and event.get("status") == "unresolved"
        )

    def _has_lineage_reinspection_report(self, event_ids: list[str]) -> bool:
        eligible_event_ids = set(event_ids)
        return any(
            str(report.get("event_id") or "") in eligible_event_ids
            and isinstance(report.get("prior_report_task_call_ids"), list)
            and bool(report["prior_report_task_call_ids"])
            and bool(str(report.get("challenge") or "").strip())
            for report in self._completed_inspection_reports()
        )

    @staticmethod
    def _reflection_matches_progress(
        reflection: dict[str, Any] | None,
        latest_progress: dict[str, Any] | None,
    ) -> bool:
        return bool(
            isinstance(reflection, dict)
            and isinstance(latest_progress, dict)
            and reflection.get("based_on_progress") == latest_progress
        )

    def _lineage_reinspection_instruction(self, unresolved_event_ids: list[str]) -> str:
        latest_reports: dict[str, dict[str, Any]] = {}
        for report in self._completed_inspection_reports():
            event_id = str(report.get("event_id") or "")
            if event_id:
                latest_reports[event_id] = report
        hints = [
            {
                "event_id": event_id,
                "latest_report_task_call_id": latest_reports.get(event_id, {}).get("task_call_id"),
                "coverage_gaps": latest_reports.get(event_id, {}).get("coverage_gaps", []),
            }
            for event_id in unresolved_event_ids
        ]
        return (
            "至少选择一个 unresolved event 做有依据的复查。InspectionTask 必须在 "
            "prior_report_task_call_ids 引用该 event 的最新 incomplete 报告，用 challenge "
            "说明要推翻或确认的疑点，并增加新的局部 verification target。可复查事件："
            f"{json.dumps(hints, ensure_ascii=False)}"
        )

    def _completed_evidence_plan(self) -> dict[str, Any] | None:
        for record in reversed(list(self._state.completed_tool_calls.values())):
            if record.name != "evidence_plan" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            plan = payload.get("research_plan") if payload is not None else None
            if isinstance(plan, dict):
                return plan
        return None

    def _completed_inspection_reports(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        seen_task_call_ids: set[str] = set()
        for record in self._state.completed_tool_calls.values():
            if record.name != "evidence_inspection_report" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            report = payload.get("inspection_report") if isinstance(payload, dict) else None
            if not isinstance(report, dict):
                continue
            task_call_id = str(report.get("task_call_id") or "")
            if not task_call_id or task_call_id in seen_task_call_ids:
                continue
            seen_task_call_ids.add(task_call_id)
            reports.append(report)
        return reports

    def _prepare_scout_report_request(self, request: Any) -> tuple[Any, ToolMessage | None]:
        """Turn Scout-authored semantic decisions into the canonical page report."""

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") != "evidence_scout_report":
            return request, None
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict) or "decisions" not in arguments:
            return request, None
        page = self._active_scout_page_payload
        plan_payload = self._completed_evidence_plan()
        if page is None or plan_payload is None:
            return request, self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_page_required",
                next_action=(
                    "先成功读取当前 task 指定的一个 catalog page，再提交该页逐 segment decisions。"
                ),
            )
        catalog = page.get("catalog")
        catalog_id = catalog.get("catalog_id") if isinstance(catalog, dict) else None
        segments = page.get("segments")
        try:
            decisions = [
                ScoutSegmentDecision.model_validate(item) for item in arguments.get("decisions", [])
            ]
            report = build_scout_report(
                research_plan=ResearchPlan.model_validate(plan_payload),
                catalog_id=str(catalog_id or ""),
                shard_cursor=str(page.get("cursor", "0")),
                next_cursor=(
                    str(page["next_cursor"]) if page.get("next_cursor") is not None else None
                ),
                library_id=str(page.get("library_id") or ""),
                asset_id=str(page.get("asset_id") or ""),
                page_segments=segments if isinstance(segments, list) else [],
                decisions=decisions,
                research_round=1,
            )
        except ValueError as exc:
            return request, self._rejection(
                request,
                phase="content_mapping",
                reason_code="oracle_scout_decisions_invalid",
                next_action=(
                    "修正当前页 decisions 后重试；每个真实 segment_id 必须恰好提交一次。"
                    f"全部已发现问题：{exc}"
                ),
            )
        return (
            self._replace_tool_call_arguments(
                request,
                tool_call,
                {"scout_report": report.model_dump(mode="json")},
            ),
            None,
        )

    def _prepare_event_map_request(self, request: Any) -> tuple[Any, ToolMessage | None]:
        """Resolve main-Agent groupings against immutable Scout frontier facts."""

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") != "evidence_event_map":
            return request, None
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict) or "decisions" not in arguments:
            return request, None
        plan_payload = self._completed_evidence_plan()
        report_payloads = self._completed_scout_reports()
        if plan_payload is None or not report_payloads:
            return request, self._rejection(
                request,
                phase="event_modeling",
                reason_code="oracle_scout_reports_required",
                next_action="先完成 ScoutReport，再提交基于真实 frontier_ids 的事件分组。",
            )
        try:
            decisions = [
                EventGroupingDecision.model_validate(item)
                for item in arguments.get("decisions", [])
            ]
            plan = ResearchPlan.model_validate(plan_payload)
            events = build_event_map(
                research_plan=plan,
                scout_reports=[ScoutReport.model_validate(item) for item in report_payloads],
                decisions=decisions,
            )
        except ValueError as exc:
            available_frontier_ids = sorted(self._retained_frontier_ids(report_payloads))
            return request, self._rejection(
                request,
                phase="event_modeling",
                reason_code="oracle_event_grouping_invalid",
                next_action=(
                    "只使用已完成 ScoutReport 中的真实 frontier_ids 修正事件分组后重试；"
                    f"全部已发现问题：{exc}。本 run 可用 frontier_ids："
                    f"{json.dumps(available_frontier_ids, ensure_ascii=False)}"
                ),
            )
        return (
            self._replace_tool_call_arguments(
                request,
                tool_call,
                {
                    "research_plan": plan.model_dump(mode="json"),
                    "events": [event.model_dump(mode="json") for event in events],
                },
            ),
            None,
        )

    def _inject_immutable_research_plan(self, request: Any) -> Any:
        """Compose a checkpoint from the run's immutable plan and model progress.

        The model owns the mutable per-slot progress only.  Requiring it to
        reproduce the complete plan at every checkpoint caused both JSON-string
        arguments and plan drift in real Oracle traces.
        """

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") not in {
            "evidence_event_map",
            "evidence_progress",
            "evidence_exclusion_review",
            "evidence_scout_report",
            "evidence_package",
        }:
            return request
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        tool_name = str(tool_call.get("name") or "")
        if not isinstance(arguments, dict):
            return request
        plan = self._completed_evidence_plan()
        if plan is None:
            return request
        updated_arguments = dict(arguments)
        if tool_name == "evidence_exclusion_review":
            reviews = updated_arguments.pop("reviews", None)
            if not isinstance(reviews, list):
                return request
            return self._replace_tool_call_arguments(
                request,
                tool_call,
                {"exclusion_review": {"research_plan": plan, "reviews": reviews}},
            )
        if tool_name == "evidence_scout_report":
            if "scout_report" in updated_arguments:
                return request
            report = dict(updated_arguments)
            report["research_plan"] = plan
            if self._active_scout_page_segment_ids is not None:
                report["page_segment_ids"] = self._active_scout_page_segment_ids
            return self._replace_tool_call_arguments(request, tool_call, {"scout_report": report})
        if tool_name == "evidence_event_map":
            if "events" not in updated_arguments:
                return request
            updated_arguments["research_plan"] = plan
            return self._replace_tool_call_arguments(request, tool_call, updated_arguments)
        if tool_name == "evidence_package":
            latest_progress = self._completed_research_progress()
            updated_arguments.pop("progress", None)
            updated_arguments.pop("events", None)
            updated_arguments.pop("research_state", None)
            updated_arguments["research_state"] = latest_progress or {"plan": plan}
            return self._replace_tool_call_arguments(request, tool_call, updated_arguments)
        supplied_state = updated_arguments.pop("research_state", None)
        if isinstance(supplied_state, dict):
            progress = supplied_state.get("progress")
            events = supplied_state.get("events")
        else:
            progress = updated_arguments.pop("progress", None)
            events = updated_arguments.pop("events", [])
        if not isinstance(progress, list):
            return request
        updated_arguments.pop("progress", None)
        updated_arguments.pop("events", None)
        research_state = {
            "plan": plan,
            "progress": progress,
            "events": self._canonical_research_events(events),
        }
        inspection_reports = self._completed_inspection_reports()
        if inspection_reports:
            research_state["inspection_reports"] = inspection_reports
        updated_arguments["research_state"] = research_state
        return self._replace_tool_call_arguments(request, tool_call, updated_arguments)

    def _canonical_research_events(self, submitted_events: Any) -> list[dict[str, Any]]:
        """Keep canonical Event Map facts while accepting main-Agent dispositions."""

        event_map = self._completed_evidence_event_map()
        canonical_events = event_map.get("events", []) if isinstance(event_map, dict) else []
        if not isinstance(canonical_events, list) or not canonical_events:
            return submitted_events if isinstance(submitted_events, list) else []
        updates = {
            str(item.get("event_id")): item
            for item in submitted_events or []
            if isinstance(item, dict) and str(item.get("event_id") or "").strip()
        }
        events: list[dict[str, Any]] = []
        for canonical in canonical_events:
            if not isinstance(canonical, dict):
                continue
            event = dict(canonical)
            update = updates.get(str(event.get("event_id") or ""))
            if update is not None:
                for field_name in ("status", "rationale"):
                    if field_name in update:
                        event[field_name] = update[field_name]
            events.append(event)
        return events

    def _inject_latest_research_progress(self, request: Any) -> Any:
        """Bind reflection to the latest factual checkpoint, not model-replayed state."""

        tool_call = self._tool_call(request)
        if str(tool_call.get("name") or "") != "evidence_reflection":
            return request
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return request
        progress = self._completed_research_progress()
        if progress is None:
            return request
        reflection = dict(arguments)
        reflection.pop("based_on_progress", None)
        reflection["based_on_progress"] = progress
        return self._replace_tool_call_arguments(request, tool_call, {"reflection": reflection})

    @staticmethod
    def _replace_tool_call_arguments(
        request: Any,
        tool_call: dict[str, Any],
        arguments: dict[str, Any],
    ) -> Any:
        updated_tool_call = dict(tool_call)
        updated_tool_call["args"] = arguments
        updated_tool_call.pop("arguments", None)
        override = getattr(request, "override", None)
        if callable(override):
            return override(tool_call=updated_tool_call)
        request.tool_call = updated_tool_call
        return request

    def _inject_active_scout_page_size(self, request: Any) -> Any:
        """Make the delegated catalog-shard contract authoritative at the tool boundary."""

        if self._active_scout_page_size is None or not self._is_scout_page_request(request):
            return request
        tool_call = self._tool_call(request)
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        if not isinstance(arguments, dict):
            return request
        updated_arguments = dict(arguments)
        updated_arguments["page_size"] = self._active_scout_page_size
        return self._replace_tool_call_arguments(request, tool_call, updated_arguments)

    @staticmethod
    def _scout_catalog_page_size(tool_call: dict[str, Any]) -> int | None:
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return None
        description = arguments.get("description")
        payload = _parse_json_object(description) if isinstance(description, str) else None
        if payload is None:
            return None
        try:
            task = ScoutTask.model_validate(payload)
        except ValueError:
            return None
        return task.page_size if task.phase == "catalog_mapping" else None

    def _inject_catalog_mapping_scout_task(self, request: Any) -> Any:
        """Replace free-form Scout handoffs with the next ledger-derived page task.

        The main agent must not decide the target cursor one page at a time.
        It receives one complete catalog mapping round, while the runtime
        derives each bounded page from factual list-tool results.  This keeps
        the original question as background only and makes the immediate goal
        high-recall candidate collection.
        """

        tool_call = self._tool_call(request)
        if self._task_role(tool_call) != "evidence-scout":
            return request
        plan = self._completed_evidence_plan()
        if plan is None:
            return request
        pending_cursor = self._pending_catalog_shard_cursor()
        if pending_cursor is None and self._has_completed_content_mapping():
            return request
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return request
        description = arguments.get("description")
        description_payload = (
            _parse_json_object(description) if isinstance(description, str) else None
        )
        description_payload = description_payload or {}
        original_question = str(
            description_payload.get("original_question")
            or description
            or "Oracle evidence collection"
        ).strip()
        scope = description_payload.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        requirement_ids = [
            str(item.get("id") or "").strip()
            for item in plan.get("subquestions", [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        if not requirement_ids:
            return request
        contract = ScoutTask(
            phase="catalog_mapping",
            research_round=1,
            original_question=original_question,
            scope=scope,
            unresolved_requirement_ids=requirement_ids,
            candidate_policy="high_recall",
            objective=(
                "只审阅指定目录页并高召回保留可能影响答案的片段；"
                "不得在此阶段判定最终答案、最终次数或静默排除弱信号。"
            ),
            catalog_id=self._known_catalog_id(),
            cursor=pending_cursor or "0",
            page_size=40,
        )
        updated_arguments = dict(arguments)
        updated_arguments["description"] = contract.model_dump_json()
        updated_tool_call = dict(tool_call)
        updated_tool_call["args"] = updated_arguments
        updated_tool_call.pop("arguments", None)
        override = getattr(request, "override", None)
        if callable(override):
            return override(tool_call=updated_tool_call)
        request.tool_call = updated_tool_call
        return request

    @staticmethod
    def _catalog_page_segment_ids(result: ToolMessage | Any) -> list[str]:
        """Read immutable page identities from a successful catalog tool result."""

        payload = parse_tool_payload(_message_tool_result(result))
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            return []
        return [
            str(segment.get("segment_id"))
            for segment in segments
            if isinstance(segment, dict) and str(segment.get("segment_id") or "").strip()
        ]

    @staticmethod
    def _catalog_page_payload(result: ToolMessage | Any) -> dict[str, Any] | None:
        payload = parse_tool_payload(_message_tool_result(result))
        return payload if isinstance(payload, dict) else None

    def _completed_research_progress(self) -> dict[str, Any] | None:
        for record in reversed(list(self._state.completed_tool_calls.values())):
            if record.name != "evidence_progress" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            research_state = payload.get("research_state") if payload is not None else None
            if isinstance(research_state, dict):
                return research_state
        return None

    def _has_inspector_result_after_latest_progress(self) -> bool:
        """Return whether new Inspector facts have not been reviewed by the main Agent."""

        records = list(self._state.completed_tool_calls.values())
        latest_progress_index = next(
            (
                index
                for index in range(len(records) - 1, -1, -1)
                if records[index].name == "evidence_progress"
                and not _tool_result_has_error(records[index].result)
            ),
            None,
        )
        if latest_progress_index is None:
            return False
        return any(
            self._task_role_from_record(record) == "evidence-inspector"
            and not _tool_result_has_error(record.result)
            for record in records[latest_progress_index + 1 :]
        )

    def _completed_research_reflection(self) -> dict[str, Any] | None:
        for record in reversed(list(self._state.completed_tool_calls.values())):
            if record.name != "evidence_reflection" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            reflection = payload.get("reflection") if payload is not None else None
            if isinstance(reflection, dict):
                return reflection
        return None

    def _completed_evidence_event_map(self) -> dict[str, Any] | None:
        for record in reversed(list(self._state.completed_tool_calls.values())):
            if record.name != "evidence_event_map" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            event_map = payload.get("event_map") if payload is not None else None
            if isinstance(event_map, dict):
                return event_map
        return None

    def _requires_exhaustive_event_coverage(self) -> bool:
        plan = self._completed_evidence_plan()
        return bool(
            isinstance(plan, dict)
            and any(
                isinstance(subquestion, dict)
                and subquestion.get("requires_exhaustive_event_coverage") is True
                for subquestion in plan.get("subquestions", [])
            )
        )

    def _completed_exclusion_review(self) -> dict[str, Any] | None:
        for record in reversed(list(self._state.completed_tool_calls.values())):
            if record.name != "evidence_exclusion_review" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            review = payload.get("exclusion_review") if payload is not None else None
            if isinstance(review, dict):
                return review
        return None

    def _completed_scout_reports(self) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for record in self._state.completed_tool_calls.values():
            if record.name != "evidence_scout_report" or _tool_result_has_error(record.result):
                continue
            payload = parse_tool_payload(record.result)
            report = payload.get("scout_report") if payload is not None else None
            if isinstance(report, dict):
                reports.append(report)
        return reports

    @staticmethod
    def _material_exclusion_frontier_ids(reports: Sequence[dict[str, Any]]) -> set[str]:
        return {
            str(item.get("frontier_id"))
            for report in reports
            for item in report.get("material_exclusions", [])
            if isinstance(item, dict) and str(item.get("frontier_id") or "").strip()
        }

    @staticmethod
    def _retained_frontier_ids(reports: Sequence[dict[str, Any]]) -> set[str]:
        return {
            str(item.get("frontier_id"))
            for report in reports
            for item in report.get("locators", [])
            if isinstance(item, dict) and str(item.get("frontier_id") or "").strip()
        }

    @staticmethod
    def _reviewed_exclusion_frontier_ids(review: dict[str, Any] | None) -> set[str]:
        if review is None:
            return set()
        reviews = review.get("reviews", [])
        return {
            str(item.get("frontier_id"))
            for item in reviews
            if isinstance(item, dict) and str(item.get("frontier_id") or "").strip()
        }

    @staticmethod
    def _reopened_exclusion_frontier_ids(review: dict[str, Any] | None) -> set[str]:
        if review is None:
            return set()
        reviews = review.get("reviews", [])
        return {
            str(item.get("frontier_id"))
            for item in reviews
            if isinstance(item, dict)
            and item.get("main_disposition") == "reopen"
            and str(item.get("frontier_id") or "").strip()
        }

    def _has_completed_tool(self, tool_name: str) -> bool:
        return any(
            record.name == tool_name and not _tool_result_has_error(record.result)
            for record in self._state.completed_tool_calls.values()
        )

    def _has_completed_task_with_same_arguments(self, tool_call: dict[str, Any]) -> bool:
        arguments = _normalized_tool_call_arguments(tool_call)
        return any(
            record.name == "task"
            and _normalized_serialized_tool_arguments(record.arguments) == arguments
            and not _tool_result_has_error(record.result)
            for record in self._state.completed_tool_calls.values()
        )

    @staticmethod
    def _tool_call(request: Any) -> dict[str, Any]:
        value = getattr(request, "tool_call", {}) or {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _task_role(tool_call: dict[str, Any]) -> str | None:
        if str(tool_call.get("name") or "") != "task":
            return None
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        if not isinstance(arguments, dict):
            return None
        subagent_type = str(arguments.get("subagent_type") or "").strip()
        return subagent_type if subagent_type in {"evidence-scout", "evidence-inspector"} else None

    @staticmethod
    def _inspection_task_from_delegation(tool_call: dict[str, Any]) -> InspectionTask | None:
        """Read the one structured fact-verification contract sent to an Inspector."""

        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(arguments, str):
            arguments = _parse_json_object(arguments) or {}
        if not isinstance(arguments, dict):
            return None
        description = arguments.get("description")
        if not isinstance(description, str):
            return None
        try:
            payload = _parse_json_object(description)
            if payload is None:
                return None
            if payload.get("task_type") == "InspectionTask":
                payload = {key: value for key, value in payload.items() if key != "task_type"}
            return InspectionTask.model_validate(payload)
        except ValueError:
            return None

    @staticmethod
    def _inspection_task_matches_event_map(
        task: InspectionTask,
        event_map: dict[str, Any] | None,
    ) -> bool:
        events = event_map.get("events", []) if isinstance(event_map, dict) else []
        event = next(
            (
                item
                for item in events
                if isinstance(item, dict) and item.get("event_id") == task.event_id
            ),
            None,
        )
        if event is None:
            return False
        if not set(task.frontier_ids).issubset(set(event.get("frontier_ids", []))):
            return False
        if not set(task.requirement_ids).issubset(set(event.get("requirement_ids", []))):
            return False
        event_range = event.get("time_range")
        if event_range is None:
            return task.time_range is None
        if not isinstance(event_range, dict) or not isinstance(task.time_range, dict):
            return False
        return task.time_range["start_seconds"] >= event_range.get(
            "start_seconds", float("inf")
        ) and task.time_range["end_seconds"] <= event_range.get("end_seconds", float("-inf"))

    @staticmethod
    def _targeted_scout_task_from_delegation(tool_call: dict[str, Any]) -> ScoutTask | None:
        arguments = tool_call.get("args", tool_call.get("arguments", {}))
        if not isinstance(arguments, dict):
            return None
        description = arguments.get("description")
        if not isinstance(description, str):
            return None
        try:
            task = ScoutTask.model_validate_json(description)
        except ValueError:
            return None
        return task if task.phase == "targeted_research" and task.research_round >= 2 else None

    def _has_completed_scope_discovery(self) -> bool:
        """The first successful Scout task before planning establishes scope discovery."""

        for record in self._state.completed_tool_calls.values():
            if record.name == "evidence_plan" and not _tool_result_has_error(record.result):
                return False
            if (
                record.name == "task"
                and not _tool_result_has_error(record.result)
                and self._task_role_from_record(record) == "evidence-scout"
            ):
                return True
        return False

    def _has_completed_content_mapping(self) -> bool:
        """Content mapping starts after a successful catalog page is actually read."""

        plan_seen = False
        for record in self._state.completed_tool_calls.values():
            if record.name == "evidence_plan" and not _tool_result_has_error(record.result):
                plan_seen = True
                continue
            if (
                plan_seen
                and record.name in {"list_video_segments", "list_doc_segments"}
                and not _tool_result_has_error(record.result)
                and (_parse_json_object(record.arguments) or {}).get("mode") == "page"
            ):
                return True
        return False

    def _pending_catalog_shard_cursor(self) -> str | None:
        """Return the next catalog cursor not yet consumed by a Scout page.

        A page is a bounded handoff, not completion of the whole content map.
        The cursor is derived from actual successful list-tool results, rather
        than trusting an LLM's textual Scout summary.
        """

        if self._completed_evidence_event_map() is not None:
            return None
        scout_reports = self._completed_scout_reports()
        if scout_reports:
            next_cursor = scout_reports[-1].get("next_cursor")
            return str(next_cursor).strip() if next_cursor is not None else None
        if self._known_catalog_id() is not None:
            return "0"

        consumed_cursors: set[str] = set()
        advertised_cursors: list[str] = []
        rejected_page_cursors: list[str] = []
        successful_page_seen = False
        for record in self._state.completed_tool_calls.values():
            if record.name not in {"list_video_segments", "list_doc_segments"}:
                continue
            arguments = _parse_json_object(record.arguments)
            if arguments is None or str(arguments.get("mode") or "") != "page":
                continue
            result = parse_tool_payload(record.result)
            if _tool_result_has_error(record.result):
                rejected_page_cursors.append(str(arguments.get("cursor") or "0").strip() or "0")
                continue
            successful_page_seen = True
            cursor = str(arguments.get("cursor") or "").strip()
            if cursor:
                consumed_cursors.add(cursor)
            next_cursor = str((result or {}).get("next_cursor") or "").strip()
            if next_cursor:
                advertised_cursors.append(next_cursor)
        pending_cursor = next(
            (cursor for cursor in reversed(advertised_cursors) if cursor not in consumed_cursors),
            None,
        )
        if pending_cursor:
            return pending_cursor
        retry_cursor = next(
            (
                cursor
                for cursor in reversed(rejected_page_cursors)
                if cursor not in consumed_cursors
            ),
            None,
        )
        if retry_cursor:
            return retry_cursor
        if not successful_page_seen:
            return None
        return None

    def _known_catalog_id(self) -> str | None:
        """Return the latest successful catalog id for an Oracle segment map."""

        catalog_id: str | None = None
        for record in self._state.completed_tool_calls.values():
            if record.name not in {"list_video_segments", "list_doc_segments"}:
                continue
            arguments = _parse_json_object(record.arguments)
            if arguments is None or str(arguments.get("mode") or "") != "map":
                continue
            if _tool_result_has_error(record.result):
                continue
            result = parse_tool_payload(record.result)
            catalog = result.get("catalog")
            candidate = (
                str(catalog.get("catalog_id") or "").strip() if isinstance(catalog, dict) else ""
            )
            if candidate:
                catalog_id = candidate
        return catalog_id

    def _known_catalog_tool_name(self) -> str | None:
        """Return the tool that created the latest successful Oracle catalog."""

        tool_name: str | None = None
        for record in self._state.completed_tool_calls.values():
            if record.name not in {"list_video_segments", "list_doc_segments"}:
                continue
            arguments = _parse_json_object(record.arguments)
            if arguments is None or str(arguments.get("mode") or "") != "map":
                continue
            if _tool_result_has_error(record.result):
                continue
            result = parse_tool_payload(record.result)
            catalog = result.get("catalog")
            if isinstance(catalog, dict) and str(catalog.get("catalog_id") or "").strip():
                tool_name = record.name
        return tool_name

    @classmethod
    def _task_role_from_record(cls, record: ToolCallRecord) -> str | None:
        if record.name != "task":
            return None
        arguments = _parse_json_object(record.arguments)
        return cls._task_role({"name": "task", "args": arguments or {}})

    def _has_inspection_coverage_gap(self) -> bool:
        """A later Scout round needs a structured Inspector-reported gap."""

        for record in self._state.completed_tool_calls.values():
            if self._task_role_from_record(record) != "evidence-inspector":
                continue
            report = _parse_json_object(record.result)
            if report is not None and (
                bool(report.get("coverage_gap")) or bool(report.get("coverage_gaps"))
            ):
                return True
        return False

    def _rejection(
        self,
        request: Any,
        *,
        phase: str,
        next_action: str,
        reason_code: str = "oracle_phase_already_completed",
    ) -> ToolMessage:
        tool_call = self._tool_call(request)
        message = (
            f"Evidence Oracle phase '{phase}' cannot be delegated again in this run. {next_action}"
        )
        return ToolMessage(
            content=json.dumps(
                {
                    "tool": str(tool_call.get("name") or "task"),
                    "error": {
                        "code": "tool_gate_blocked",
                        "message": message,
                        "detail": reason_code,
                    },
                    "tool_gate": {
                        "blocked": True,
                        "reason_code": reason_code,
                        "next_action": next_action,
                        "recovery": {
                            "requirement_code": "oracle_phase_transition",
                            "constraint": next_action,
                            "available_values": [],
                        },
                    },
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(tool_call.get("id") or tool_call.get("call_id") or ""),
            name=str(tool_call.get("name") or "task"),
        )


class _RepeatedWorkspaceToolCallMiddleware(AgentMiddleware):
    """Reject identical completed workspace searches so the run must make progress."""

    _TOOL_NAMES = frozenset({"grep"})
    _REASON_CODE = "duplicate_successful_workspace_tool_call"
    _CONSTRAINT = (
        "同一 grep 查询已在本 run 完成；请复用已有结果。若仍需推进，请改变查询参数，"
        "或转向已获得的候选和其他可验证路径，不要重复同一调用。"
    )

    def __init__(self, state: _RunState) -> None:
        self._state = state

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        if self._is_repeated_completed_call(request):
            return self._blocked_result(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> ToolMessage | Any:
        if self._is_repeated_completed_call(request):
            return self._blocked_result(request)
        return await handler(request)

    def _is_repeated_completed_call(self, request: Any) -> bool:
        tool_call = self._tool_call(request)
        tool_name = str(tool_call.get("name") or "")
        if tool_name not in self._TOOL_NAMES:
            return False
        arguments = _normalized_tool_call_arguments(tool_call)
        return any(
            completed.name == tool_name
            and _normalized_serialized_tool_arguments(completed.arguments) == arguments
            and not _tool_result_has_error(completed.result)
            for completed in self._state.completed_tool_calls.values()
        )

    @staticmethod
    def _tool_call(request: Any) -> dict[str, Any]:
        value = getattr(request, "tool_call", {}) or {}
        return value if isinstance(value, dict) else {}

    def _blocked_result(self, request: Any) -> ToolMessage:
        tool_call = self._tool_call(request)
        tool_name = str(tool_call.get("name") or "grep")
        return ToolMessage(
            content=json.dumps(
                {
                    "tool": tool_name,
                    "error": {
                        "code": "tool_gate_blocked",
                        "message": self._CONSTRAINT,
                        "detail": self._REASON_CODE,
                    },
                    "tool_gate": {
                        "blocked": True,
                        "reason_code": self._REASON_CODE,
                        "next_action": self._CONSTRAINT,
                        "recovery": {
                            "requirement_code": "reuse_completed_workspace_result",
                            "constraint": self._CONSTRAINT,
                            "available_values": [],
                        },
                    },
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(tool_call.get("id") or tool_call.get("call_id") or ""),
            name=tool_name,
        )


def _normalized_tool_call_arguments(tool_call: dict[str, Any]) -> str:
    arguments = tool_call.get("args")
    if arguments is None:
        arguments = tool_call.get("arguments")
    if isinstance(arguments, str):
        parsed = _parse_json_object(arguments)
        if parsed is not None:
            arguments = parsed
    return json.dumps(
        arguments if isinstance(arguments, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
    )


def _normalized_serialized_tool_arguments(arguments: str) -> str:
    parsed = _parse_json_object(arguments)
    return json.dumps(parsed or {}, ensure_ascii=False, sort_keys=True)


def _tool_result_has_error(result: str) -> bool:
    payload = _parse_json_object(result)
    return bool(payload and payload.get("error"))


class _VisualGroundLimitMiddleware(AgentMiddleware):
    """Cap visual_ground executions per run and force progress after the cap."""

    _MAX_CALLS = 3
    _LIMIT_MESSAGE = (
        "visual_ground 本轮已达到最多 3 次调用限制。不要再次调用 visual_ground，"
        "请基于当前已有证据直接回答；如果证据不足，请明确说明无法确认。"
    )
    _MODEL_NOTICE = (
        "系统限制：本轮 visual_ground 已调用 3 次，禁止再次调用该工具。"
        "请停止视觉核验，基于已有证据直接作答；证据不足时明确说明无法确认。"
    )

    def __init__(self, state: _RunState) -> None:
        self._state = state

    @staticmethod
    def _tool_call(request: Any) -> dict[str, Any]:
        value = getattr(request, "tool_call", {}) or {}
        return value if isinstance(value, dict) else {}

    def _is_visual_ground(self, request: Any) -> bool:
        return str(self._tool_call(request).get("name") or "") == "visual_ground"

    def _blocked_result(self, request: Any) -> ToolMessage:
        tool_call = self._tool_call(request)
        return ToolMessage(
            content=self._LIMIT_MESSAGE,
            tool_call_id=str(tool_call.get("id") or tool_call.get("call_id") or ""),
            name="visual_ground",
        )

    def _append_notice(self, request: Any) -> Any:
        system_message = request.system_message
        if system_message is None:
            return request.override(
                system_message=SystemMessage(
                    content_blocks=[{"type": "text", "text": self._MODEL_NOTICE}]
                )
            )
        blocks = [
            *system_message.content_blocks,
            {"type": "text", "text": f"\n\n{self._MODEL_NOTICE}"},
        ]
        return request.override(system_message=SystemMessage(content_blocks=blocks))

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if self._state.visual_ground_call_count >= self._MAX_CALLS:
            return handler(self._append_notice(request))
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if self._state.visual_ground_call_count >= self._MAX_CALLS:
            return await handler(self._append_notice(request))
        return await handler(request)

    def wrap_tool_call(
        self, request: Any, handler: Callable[[Any], ToolMessage | Any]
    ) -> ToolMessage | Any:
        if not self._is_visual_ground(request):
            return handler(request)
        if self._state.visual_ground_call_count >= self._MAX_CALLS:
            return self._blocked_result(request)
        self._state.visual_ground_call_count += 1
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Any]
    ) -> ToolMessage | Any:
        if not self._is_visual_ground(request):
            return await handler(request)
        if self._state.visual_ground_call_count >= self._MAX_CALLS:
            return self._blocked_result(request)
        self._state.visual_ground_call_count += 1
        return await handler(request)


def _append_budget_warning_to_tool_message(
    state: _RunState,
    result: ToolMessage,
) -> ToolMessage:
    if not state.budget_warning_emitted or state.budget_exhausted:
        return result
    notice = state.budget_warning_notice
    if notice is None:
        raise ValueError("budget warning requires a structured budget snapshot")
    content = _serialize_tool_message_content(result)
    payload = _parse_json_object(content)
    if payload is None:
        # Framework-owned text tools do not use the ViKnow envelope.
        envelope = (
            ToolResult.fail(
                result.name or "unknown",
                ToolErrorCode.INTERNAL_ERROR,
                content,
                data={"content": content},
            )
            if result.status == "error"
            else ToolResult.ok(result.name or "unknown", {"content": content})
        )
        payload = envelope.model_dump(mode="json", exclude_none=True)
    notices = [
        item for item in payload.get("runtime_notices", []) if item.get("kind") != "budget_warning"
    ]
    payload["runtime_notices"] = [*notices, notice.model_dump(mode="json", exclude_none=True)]
    # Runtime instructions are model presentation, never part of tool facts.
    # Retain an existing full result when the tool has already been projected.
    return _tool_message_with_projection(
        result, json.dumps(payload, ensure_ascii=False), _message_tool_result(result)
    )


def _budget_tool_notice(payload: dict[str, Any]) -> ToolRuntimeNotice:
    usage = ToolBudgetUsage(
        elapsed_seconds=payload["elapsed_seconds"],
        time_limit_seconds=payload["time_limit_seconds"],
        recursion_steps=payload["recursion_step"],
        recursion_limit=payload["max_recursion_steps"],
    )
    dimensions = payload["dimensions"]
    labels = {"recursion_steps": "递归步骤", "elapsed_time": "耗时"}
    return ToolRuntimeNotice(
        triggered_by=dimensions,
        usage=usage,
        message=(
            "运行预算提示（最近一次预算检查）："
            f"LangGraph 递归步骤 {usage.recursion_steps}/{usage.recursion_limit}"
            "（不是工具调用次数）；"
            f"耗时 {usage.elapsed_seconds}/{usage.time_limit_seconds} 秒。"
            f"触发维度：{'、'.join(labels[item] for item in dimensions)}。"
            "请根据剩余预算评估未完成任务，优先核实关键缺口；"
            "无法完成的部分应明确说明。"
        ),
    )


def _normalize_claim_tool_result(
    state: _RunState,
    tool_name: str,
    *,
    tool_call_id: str,
    result: Any,
) -> Any:
    if not isinstance(result, str):
        return result
    payload = _parse_json_object(result)
    if payload is None:
        return result
    result_payload = unwrap_tool_data(payload).get("result")
    if not isinstance(result_payload, dict):
        return result
    claims = result_payload.get("claims")
    if not isinstance(claims, list):
        return result

    changed = False
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        citation_index = _ensure_claim_citation_index(
            state,
            tool_name,
            claim,
        )
        claim["citation_index"] = citation_index
        claim["agent_citation"] = f"[{citation_index}]"
        claim_ref = _claim_ref_for_tool_claim(
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            claim=claim,
        )
        if claim_ref:
            claim["claim_ref"] = claim_ref
        changed = True

    if not changed:
        return result
    return json.dumps(payload, ensure_ascii=False)


def _project_inspect_tool_message_for_agent(
    state: _RunState,
    tool_name: str,
    result: ToolMessage,
) -> ToolMessage:
    full_result = _normalize_claim_tool_result(
        state,
        tool_name,
        tool_call_id=str(result.tool_call_id or ""),
        result=_serialize_tool_message_content(result),
    )
    if not isinstance(full_result, str):
        return result
    slim_result = _slim_inspect_tool_result(full_result)
    return _tool_message_with_projection(result, slim_result, full_result)


def _project_search_tool_message_for_agent(tool_name: str, result: ToolMessage) -> ToolMessage:
    full_result = _serialize_tool_message_content(result)
    slim_result = _slim_knowledge_search_tool_result(full_result)
    if tool_name not in _SEARCH_TOOL_NAMES:
        return result
    return _tool_message_with_projection(result, slim_result, full_result)


def _project_read_file_tool_message_for_agent(result: ToolMessage) -> ToolMessage:
    content = result.content
    content_blocks = getattr(result, "content_blocks", None)
    if not (
        _content_contains_image_block(content) or _content_contains_image_block(content_blocks)
    ):
        return result
    full_result = _serialize(content)
    slim_result = (
        "read_file cannot inspect user-uploaded images in this agent runtime. "
        "Use uploaded_image_search with the uploaded file workspace_path instead."
    )
    return _tool_message_with_projection(result, slim_result, full_result)


def _content_contains_image_block(content: Any) -> bool:
    if isinstance(content, list):
        return any(_content_block_is_image(block) for block in content)
    if isinstance(content, dict):
        return any(
            _content_contains_image_block(content.get(key))
            for key in ("content", "content_blocks", "blocks")
        )
    if isinstance(content, str):
        parsed = _parse_json_value(content)
        if parsed is not None:
            return _content_contains_image_block(parsed)
    return False


def _parse_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None


def _content_block_is_image(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = str(block.get("type") or "").strip()
    return block_type in {"image", "image_url"} or "image_url" in block


def _serialize_tool_message_content(result: ToolMessage) -> str:
    content = result.content
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        return content[0]["text"]
    return _serialize(content)


def _tool_message_with_projection(
    result: ToolMessage,
    slim_result: str,
    full_result: str,
) -> ToolMessage:
    artifact = _merge_tool_message_artifact(result.artifact, {"full_result": full_result})
    return ToolMessage(
        content=slim_result,
        tool_call_id=result.tool_call_id,
        artifact=artifact,
        status=result.status,
        additional_kwargs=dict(result.additional_kwargs),
        response_metadata=dict(result.response_metadata),
        id=result.id,
        name=result.name,
    )


def _merge_tool_message_artifact(existing: Any, updates: dict[str, Any]) -> dict[str, Any]:
    artifact = dict(existing) if isinstance(existing, dict) else {}
    artifact.update(updates)
    return artifact


def _slim_inspect_tool_result(result: str) -> str:
    payload = _parse_json_object(result)
    if payload is None:
        return result
    payload = unwrap_tool_data(payload)
    result_payload = payload.get("result")
    if not isinstance(result_payload, dict):
        return result
    slim_result: dict[str, Any] = {
        "success": result_payload.get("success"),
    }
    if result_payload.get("error"):
        slim_result["error"] = result_payload.get("error")
    claims = result_payload.get("claims")
    if isinstance(claims, list):
        slim_result["claims"] = [
            _slim_inspect_claim(
                claim, include_document_source=payload.get("tool") == "document_inspect"
            )
            for claim in claims
            if isinstance(claim, dict)
        ]
    return json.dumps(
        {
            **{
                key: payload[key]
                for key in (
                    "status",
                    "code",
                    "message",
                    "error",
                    "next_action",
                    "runtime_notices",
                    "total_pages",
                    "inspected_pages",
                    "inspected_page_indices",
                )
                if key in payload
            },
            "tool": payload.get("tool"),
            "result": slim_result,
        },
        ensure_ascii=False,
    )


def _slim_knowledge_search_tool_result(result: str) -> str:
    payload = _parse_json_object(result)
    if payload is None:
        return result
    payload = unwrap_tool_data(payload)
    evidence_items = payload.get("evidence")
    if not isinstance(evidence_items, list):
        return result
    slim_payload: dict[str, Any] = {
        **{
            key: payload[key]
            for key in ("status", "code", "message", "error", "next_action", "runtime_notices")
            if key in payload
        },
        "tool": payload.get("tool"),
        "library_id": payload.get("library_id"),
        "query": payload.get("query"),
        "evidence": [
            _slim_knowledge_search_evidence(item)
            for item in evidence_items
            if isinstance(item, dict)
        ],
    }
    return json.dumps(
        {key: value for key, value in slim_payload.items() if value not in (None, [], "")},
        ensure_ascii=False,
    )


def _slim_knowledge_search_evidence(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    asset_id = item.get("asset_id") or (
        metadata.get("asset_id") if isinstance(metadata, dict) else None
    )
    slim_item: dict[str, Any] = {
        **{
            key: item[key]
            for key in ("library_id", "evidence_id", "segment_id", "time_range")
            if key in item
        },
        "id": item.get("id"),
        "title": item.get("title"),
        "score": item.get("score"),
        "asset_id": asset_id,
    }
    parts = item.get("parts")
    if isinstance(parts, list):
        slim_parts = [_slim_knowledge_search_part(part) for part in parts if isinstance(part, dict)]
        slim_item["parts"] = [part for part in slim_parts if part]
    return {
        key: value
        for key, value in slim_item.items()
        if value not in (None, [], "", {}) and not (key == "score" and value is None)
    }


def _slim_knowledge_search_part(part: dict[str, Any]) -> dict[str, Any]:
    kind = str(part.get("kind") or "").strip()
    if not kind:
        return {}
    slim_part: dict[str, Any] = {"kind": kind}
    if kind == "text":
        text = str(part.get("text") or "").strip()
        if text:
            slim_part["text"] = text
    time_range = part.get("time_range")
    if isinstance(time_range, dict):
        start_seconds = time_range.get("start_seconds")
        end_seconds = time_range.get("end_seconds")
        if start_seconds is not None and end_seconds is not None:
            slim_part["time_range"] = {
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            }
    return slim_part


def _slim_inspect_claim(
    claim: dict[str, Any], *, include_document_source: bool = False
) -> dict[str, Any]:
    keys = (
        "claim_ref",
        "claim",
        "agent_citation",
        "citation_index",
        "display_label",
        "confidence",
        "quote_verified",
        "status",
        "verified",
        "verify_reason",
        "modalities",
        "source",
        "supports",
        "sampling_range",
    )
    compact = {key: claim.get(key) for key in keys if claim.get(key) not in (None, "")}
    source = compact.get("source")
    if isinstance(source, dict):
        if include_document_source:
            compact["source"] = {
                key: source[key] for key in ("asset_id", "anchors") if key in source
            }
        else:
            compact["source"] = _compact_claim_source(source)
    return compact


def _build_trace_tool_metadata(query: str, state: _RunState) -> dict[str, Any]:
    metadata = {
        "viknow_query": query,
        **(
            {"viknow_evidence_repair": state.repair_diagnostics} if state.repair_diagnostics else {}
        ),
        "viknow_skills_read": list(state.read_skills),
        "viknow_search_results": list(state.search_results),
        "viknow_inspect_results": {
            key: list(value) for key, value in state.inspect_results.items() if value
        },
    }
    tool_internal_model_usage = _tool_internal_model_usage_metadata(state)
    if tool_internal_model_usage:
        metadata["viknow_tool_internal_model_usage"] = tool_internal_model_usage
    metadata.update(state.answer_review_trace_metadata)
    return metadata


def _answer_review_trace_payload(answer_review: AnswerReviewResult) -> dict[str, Any]:
    return {
        "passed": answer_review.passed,
        "overall_score": answer_review.overall_score,
        "dimension_scores": answer_review.dimension_scores.model_dump(mode="json"),
        "dimension_statuses": answer_review.dimension_statuses.model_dump(mode="json"),
        "citation_policy": answer_review.citation_policy.model_dump(mode="json"),
        "risks": [risk.model_dump(mode="json") for risk in answer_review.risks],
        "claims": [claim.model_dump(mode="json") for claim in answer_review.claims],
        **(
            {"selected_choice": answer_review.selected_choice.model_dump(mode="json")}
            if answer_review.selected_choice is not None
            else {}
        ),
        "review_trace": dict(answer_review.review_trace),
    }


def _record_tool_internal_model_usage(
    state: _RunState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    usage = _structured_token_usage(payload.get("internal_model_usage"))
    calls = payload.get("internal_model_calls")
    normalized_calls = (
        [dict(call) for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []
    )
    if not usage and not normalized_calls:
        return {}
    record: dict[str, Any] = {
        "tool_call_id": str(payload.get("id") or ""),
        "tool": str(payload.get("name") or ""),
    }
    if usage:
        record["usage"] = usage
    if normalized_calls:
        record["calls"] = normalized_calls
    state.tool_internal_model_usage.append(record)
    return {"viknow_tool_internal_model_usage": _tool_internal_model_usage_metadata(state)}


def _tool_internal_model_usage_metadata(state: _RunState) -> dict[str, Any]:
    if not state.tool_internal_model_usage:
        return {}
    tool_calls = [dict(record) for record in state.tool_internal_model_usage]
    total_usage = _aggregate_structured_token_usages(
        record.get("usage") for record in tool_calls if isinstance(record.get("usage"), dict)
    )
    metadata: dict[str, Any] = {"tool_calls": tool_calls}
    if total_usage:
        metadata["total_usage"] = total_usage
    return metadata


def _aggregate_structured_token_usages(values: Iterable[object]) -> dict[str, int]:
    aggregate: dict[str, int] = {}
    for value in values:
        usage = _structured_token_usage(value)
        for key, count in usage.items():
            aggregate[key] = aggregate.get(key, 0) + count
    return aggregate


def _structured_token_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        count = _token_count(value.get(key))
        if count is not None:
            usage[key] = count
    return usage


def _record_skill_read(state: _RunState, payload: dict[str, Any]) -> None:
    if str(payload.get("name") or "") != "read_file":
        return
    arguments = _parse_json_object(str(payload.get("arguments") or ""))
    if arguments is None:
        return
    raw_path = (
        arguments.get("file_path")
        or arguments.get("path")
        or arguments.get("target_file")
        or arguments.get("filename")
    )
    if not raw_path:
        return
    skill_name = _skill_name_from_read_path(str(raw_path))
    if skill_name and skill_name not in state.read_skills:
        state.read_skills.append(skill_name)


def _skill_name_from_read_path(path: str) -> str:
    try:
        pure_path = PurePosixPath(path)
    except Exception:
        return ""
    if pure_path.name != "SKILL.md":
        return ""
    parent_name = pure_path.parent.name.strip()
    if not parent_name:
        return ""
    parts = set(pure_path.parts)
    if ".skills" not in parts and "skills" not in parts:
        return ""
    return parent_name


def _record_trace_tool_result(state: _RunState, payload: dict[str, Any]) -> None:
    tool_name = str(payload.get("name") or "")
    result = str(payload.get("result") or "")
    if tool_name in _SEARCH_TOOL_NAMES:
        search_summary = _trace_search_tool_result(result)
        if search_summary:
            state.search_results.append(search_summary)
        return
    if tool_name not in _INSPECT_TOOL_NAMES:
        return
    inspect_summary = _trace_inspect_tool_result_with_state(
        state,
        tool_name,
        str(payload.get("arguments") or ""),
        result,
    )
    if not inspect_summary:
        return
    modality = {
        "evidence_asr": "asr",
        "evidence_ocr": "ocr",
    }.get(tool_name, tool_name.removesuffix("_inspect"))
    if modality in state.inspect_results:
        state.inspect_results[modality].append(inspect_summary)


def _trace_search_tool_result(result: str) -> dict[str, Any]:
    payload = _parse_json_object(_slim_knowledge_search_tool_result(result))
    return payload if isinstance(payload, dict) else {}


def _trace_inspect_tool_result(tool_name: str, arguments: str, result: str) -> dict[str, Any]:
    return _trace_inspect_tool_result_with_state(None, tool_name, arguments, result)


def _trace_inspect_tool_result_with_state(
    state: _RunState | None,
    tool_name: str,
    arguments: str,
    result: str,
) -> dict[str, Any]:
    payload = _parse_json_object(result)
    if payload is None:
        return {}
    payload = unwrap_tool_data(payload)
    result_payload = payload.get("result")
    if not isinstance(result_payload, dict):
        return {}
    summary: dict[str, Any] = {
        "tool": tool_name,
        "success": result_payload.get("success"),
    }
    if result_payload.get("error"):
        summary["error"] = result_payload.get("error")
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        summary["evidence"] = _trace_inspect_evidence_summary(evidence)
    claims = result_payload.get("claims")
    if isinstance(claims, list):
        summary["claims"] = [
            _trace_inspect_claim_summary(state, tool_name, claim)
            for claim in claims
            if isinstance(claim, dict)
        ]
    locator_args = _parse_json_object(arguments)
    if isinstance(locator_args, dict):
        locator = {
            key: locator_args.get(key)
            for key in ("library_id", "asset_id", "evidence_id", "question")
            if locator_args.get(key) not in (None, "")
        }
        if locator:
            summary["locator"] = locator
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _trace_inspect_evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if evidence.get("id") not in (None, ""):
        summary["id"] = evidence.get("id")
    if evidence.get("title") not in (None, ""):
        summary["title"] = evidence.get("title")
    metadata = evidence.get("metadata")
    if isinstance(metadata, dict):
        summary["metadata"] = {
            key: metadata.get(key)
            for key in ("library_id", "asset_id", "file_path", "segment_ids", "chunk_id")
            if metadata.get(key) not in (None, "", [], {})
        }
    return summary


def _trace_inspect_claim_summary(
    state: _RunState | None,
    tool_name: str,
    claim: dict[str, Any],
) -> dict[str, Any]:
    summary = _slim_inspect_claim(claim)
    answer_citation = str(claim.get("agent_citation") or "").strip()
    if not answer_citation and state is not None:
        answer_citation = f"[{_ensure_claim_citation_index(state, tool_name, claim)}]"
    if answer_citation:
        summary["answer_citation"] = answer_citation
    source = claim.get("source")
    if isinstance(source, dict):
        summary["source"] = _trace_claim_source_summary(tool_name, source)
    return summary


def _ensure_claim_citation_index(
    state: _RunState,
    tool_name: str,
    claim: dict[str, Any],
) -> int:
    key = _claim_citation_key(tool_name, claim)
    citation_index = state.claim_citation_indexes.get(key)
    if citation_index is None:
        # Inspect tools number claims within one tool result.  The runtime owns
        # the answer-level citation namespace, so do not retain that local index.
        citation_index = state.next_claim_citation_index
        state.claim_citation_indexes[key] = citation_index
        state.next_claim_citation_index += 1
    return citation_index


def _trace_claim_source_summary(tool_name: str, source: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        key: source.get(key)
        for key in ("type", "asset_id", "evidence_id", "segment_id", "source_image_id")
        if source.get(key) not in (None, "", [], {})
    }
    if tool_name in {"video_inspect", "video_inspect_2_0", "evidence_asr", "evidence_ocr"}:
        for key in ("timestamp_start", "timestamp_end"):
            if source.get(key) is not None:
                summary[key] = source.get(key)
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        keys = (
            "full_doc_id",
            "chunk_id",
            "segment_content",
            "chunk_content",
            "clip_start_seconds",
            "clip_end_seconds",
            "absolute_timestamp_start",
            "absolute_timestamp_end",
        )
        metadata_summary = {
            key: metadata.get(key) for key in keys if metadata.get(key) not in (None, "", [], {})
        }
        if metadata_summary:
            summary["metadata"] = metadata_summary
    return summary


def _claim_citation_key(
    tool_name: str,
    claim: dict[str, Any],
) -> str:
    # 引用编号在 answer-level 全局命名空间分配，key 由 claim 自身身份决定，
    # 与 tool_call_id 无关：同一条 claim 会被投影路径（模型侧 tool_call_id）
    # 与事件流路径（LangGraph run_id）先后处理，若 key 里拼接了 tool_call_id，
    # 两条路径会对同一 claim 命中不同 key，各分配一次编号（双编号问题）。
    # claim_id 由工具生成且全局唯一；无 claim_id 时用内容哈希兜底。
    claim_id = str(claim.get("claim_id") or "")
    if claim_id:
        return claim_id
    raw = json.dumps(
        {
            "tool": tool_name,
            "claim": claim.get("claim"),
            "source": claim.get("source"),
            "segment_id": claim.get("segment_id"),
            "source_segments": claim.get("source_segments"),
            "source_image_id": claim.get("source_image_id"),
            "timestamp_start": claim.get("timestamp_start"),
            "timestamp_end": claim.get("timestamp_end"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    claim_key = f"claim_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"
    return claim_key


def _claim_ref_for_tool_claim(
    *,
    run_id: str,
    tool_call_id: str,
    claim: dict[str, Any],
) -> str:
    if not run_id or not tool_call_id:
        return ""
    claim_id = str(claim.get("claim_id") or "")
    if claim_id:
        return f"claim:{run_id}:{tool_call_id}:{claim_id}"
    citation_index = claim.get("citation_index")
    if isinstance(citation_index, int):
        return f"claim:{run_id}:{tool_call_id}:{citation_index}"
    return ""


def _evidence_bundle_from_tool_calls(
    tool_calls: Sequence[ToolCallRecord],
) -> tuple[list[Evidence], list[dict[str, Any]]]:
    evidence: list[Evidence] = []
    evidence_relations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tool_call in tool_calls:
        payload = _parse_json_object(tool_call.result)
        if payload is None:
            continue
        if tool_call.name == "knowledge_search":
            payload = unwrap_tool_data(payload)
            for item in payload.get("evidence") or []:
                _append_evidence(evidence, seen_ids, item)
            relations = payload.get("evidence_relations")
            if isinstance(relations, list):
                evidence_relations.extend(item for item in relations if isinstance(item, dict))
        if tool_call.name in {"video_inspect", "video_inspect_2_0"}:
            payload = unwrap_tool_data(payload)
            _append_evidence(evidence, seen_ids, payload.get("evidence"))
            for item in payload.get("evidence_list") or []:
                _append_evidence(evidence, seen_ids, item)
    return evidence, evidence_relations


def _is_counting_question(question: str) -> bool:
    return bool(_COUNTING_QUESTION_RE.search(question or ""))


def _is_static_visible_count_question(question: str) -> bool:
    if not _is_counting_question(question):
        return False
    return not _NON_STATIC_COUNT_RE.search(question)


def _auto_visual_ground_args(
    question: str,
    tool_calls: Iterable[ToolCallRecord],
) -> dict[str, Any] | None:
    if not _is_static_visible_count_question(question):
        return None
    calls = list(tool_calls)

    for tool_call in reversed(calls):
        if tool_call.name not in {"video_inspect", "video_inspect_2_0"}:
            continue
        payload = _parse_json_object(tool_call.result)
        if payload is None:
            continue
        if tool_call.name == "video_inspect":
            payload = unwrap_tool_data(payload)
        evidence = payload.get("evidence")
        if _evidence_payload_has_video(evidence):
            return {"query": _visual_ground_query(question), "evidence": evidence}
        locator_args = _locator_visual_ground_args(tool_call.arguments, question)
        if locator_args is not None:
            return locator_args
        for item in payload.get("evidence_list") or []:
            if _evidence_payload_has_video(item):
                return {"query": _visual_ground_query(question), "evidence": item}

    for tool_call in reversed(calls):
        if tool_call.name not in {"knowledge_search", "list_video_segments"}:
            continue
        payload = _parse_json_object(tool_call.result)
        if payload is None:
            continue
        payload = unwrap_tool_data(payload)
        for item in payload.get("evidence") or []:
            if _evidence_payload_has_video(item):
                return {"query": _visual_ground_query(question), "evidence": item}
    return None


def _locator_visual_ground_args(arguments: str | None, question: str) -> dict[str, Any] | None:
    args = _parse_json_object(arguments)
    if args is None:
        return None
    library_id = args.get("library_id")
    asset_id = args.get("asset_id")
    evidence_id = args.get("evidence_id")
    if not library_id or not asset_id or not evidence_id:
        return None
    payload: dict[str, Any] = {
        "query": _visual_ground_query(question),
        "library_id": library_id,
        "asset_id": asset_id,
        "evidence_id": evidence_id,
    }
    for key in ("time_range", "title", "frame_time"):
        if args.get(key) not in (None, ""):
            payload[key] = args.get(key)
    return payload


def _visual_ground_query(question: str) -> str:
    normalized = " ".join(str(question or "").split())
    return normalized or "图中目标有几个？"


def _evidence_payload_has_video(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    parts = value.get("parts")
    if not isinstance(parts, list):
        return False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if str(part.get("kind") or "").lower() == "video":
            return True
    return False


def _tool_by_name(tools: Sequence[Any], name: str) -> Any | None:
    for candidate_tool in tools:
        if str(getattr(candidate_tool, "name", "") or "") == name:
            return candidate_tool
    return None


async def _ainvoke_tool_for_auto_grounding(tool: Any, args: dict[str, Any]) -> str:
    ainvoke = getattr(tool, "ainvoke", None)
    if callable(ainvoke):
        result = await ainvoke(args)
    else:
        invoke = getattr(tool, "invoke", None)
        if not callable(invoke):
            raise RuntimeError("visual_ground tool is not invokable")
        result = await asyncio.to_thread(invoke, args)
    return _auto_tool_result_content(result)


def _auto_tool_result_content(result: Any) -> str:
    if isinstance(result, ToolMessage):
        content = result.content
        return content if isinstance(content, str) else _serialize(content)
    if isinstance(result, tuple) and result:
        content = result[0]
        return content if isinstance(content, str) else _serialize(content)
    if isinstance(result, str):
        return result
    return _serialize(result)


def _visual_ground_retry_context(claims: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("tool") != "visual_ground":
            continue
        token = str(claim.get("agent_citation") or "").strip()
        claim_text = str(claim.get("claim") or "").strip()
        if not token or not claim_text:
            continue
        lines.append(f"- {token} {claim_text}")
    return "\n".join(lines)


def _answer_review_citations(
    resolved_citations: Sequence[Any],
    claim_bundle: list[dict[str, Any]],
    *,
    current_run_id: str | None = None,
) -> list[AnswerReviewCitation]:
    citations: list[AnswerReviewCitation] = []
    for citation in resolved_citations:
        index = getattr(citation, "index", 0)
        bundle_item = claim_bundle[index - 1] if 1 <= index <= len(claim_bundle) else {}
        if not bundle_item:
            continue
        citations.append(
            AnswerReviewCitation(
                marker=str(getattr(citation, "marker", "")),
                claim_id=str(bundle_item.get("claim_id") or ""),
                citation_index=bundle_item.get("citation_index")
                if isinstance(bundle_item.get("citation_index"), int)
                else None,
                tool=str(bundle_item.get("tool") or ""),
                claim=str(bundle_item.get("claim") or ""),
                confidence=bundle_item.get("confidence")
                if isinstance(bundle_item.get("confidence"), int | float)
                else None,
                quote_verified=bundle_item.get("quote_verified")
                if isinstance(bundle_item.get("quote_verified"), bool)
                else None,
                verify_reason=str(bundle_item.get("verify_reason") or ""),
                source=bundle_item.get("source")
                if isinstance(bundle_item.get("source"), dict)
                else {},
                render=bundle_item.get("render")
                if isinstance(bundle_item.get("render"), dict)
                else {},
                claim_ref=str(bundle_item.get("claim_ref") or "")
                or _current_claim_ref(current_run_id, bundle_item),
            )
        )
    return citations


def _add_published_historical_citations(
    citations: list[AnswerReviewCitation],
    published_citations: dict[str, dict[str, Any]],
    claim_bundle: list[dict[str, Any]],
) -> list[AnswerReviewCitation]:
    existing_markers = {citation.marker for citation in citations}
    claims_by_ref = {
        str(claim.get("claim_ref") or ""): claim for claim in claim_bundle if claim.get("claim_ref")
    }
    for marker, payload in published_citations.items():
        citation_marker = f"[{marker}]"
        if citation_marker in existing_markers:
            continue
        claim = claims_by_ref.get(str(payload.get("claim_ref") or ""))
        if claim is None:
            continue
        citations.append(
            AnswerReviewCitation(
                marker=citation_marker,
                claim_id=str(claim.get("claim_id") or ""),
                citation_index=claim.get("citation_index")
                if isinstance(claim.get("citation_index"), int)
                else None,
                tool=str(claim.get("tool") or ""),
                claim=str(claim.get("claim") or ""),
                confidence=claim.get("confidence")
                if isinstance(claim.get("confidence"), int | float)
                else None,
                quote_verified=claim.get("quote_verified")
                if isinstance(claim.get("quote_verified"), bool)
                else None,
                verify_reason=str(claim.get("verify_reason") or ""),
                source=claim.get("source") if isinstance(claim.get("source"), dict) else {},
                render=claim.get("render") if isinstance(claim.get("render"), dict) else {},
                claim_ref=str(claim.get("claim_ref") or ""),
            )
        )
    return citations


def _citation_error_marker(error: str) -> str:
    match = re.search(r"\[(\d+)\]", error)
    return match.group(1) if match is not None else ""


def _historical_claim_evidence(claims: Sequence[HistoricalClaim]) -> list[Evidence]:
    return [
        Evidence(
            id=claim.claim_ref,
            source="historical_claim",
            title=claim.display_label or claim.tool,
            metadata={
                "tool": claim.tool,
                "claim": claim.claim,
                "source": claim.source,
            },
        )
        for claim in claims
    ]


def _aliased_historical_claim_bundle(state: _RunState) -> list[dict[str, Any]]:
    bundle = historical_claim_bundle(state.historical_claims)
    alias_by_ref = {
        claim_ref: alias for alias, claim_ref in state.historical_claim_alias_refs.items()
    }
    if not alias_by_ref:
        return bundle
    aliased: list[dict[str, Any]] = []
    for item in bundle:
        claim_ref = str(item.get("claim_ref") or "")
        alias = alias_by_ref.get(claim_ref)
        if not alias:
            aliased.append(item)
            continue
        next_item = dict(item)
        next_item["agent_citation"] = f"[{alias}]"
        next_item["citation_token"] = f"[{alias}]"
        aliased.append(next_item)
    return aliased


def _answer_citation_metadata(answer: str, completed_tool_calls: Sequence[ToolCallRecord]) -> dict:
    current_claims = collect_claim_bundle(completed_tool_calls)
    _, citations = _final_answer_with_citations(
        answer,
        current_claims=current_claims,
        historical_claims=[],
        historical_message_citation_map=HistoricalMessageCitationMap(),
    )
    return citations


def _final_answer_with_citations(
    answer: str,
    *,
    current_claims: list[dict[str, Any]],
    historical_claims: list[HistoricalClaim],
    historical_claim_alias_refs: dict[str, str] | None = None,
    historical_message_citation_map: HistoricalMessageCitationMap | None = None,
    current_run_id: str | None = None,
    current_resolve_result: Any | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    citations: dict[str, dict[str, Any]] = {}
    final_answer = answer
    next_marker = 1
    resolve_result = current_resolve_result or resolve_citations(answer, current_claims)
    for citation in resolve_result.citations:
        marker_index = citation_marker_index(citation.marker)
        if marker_index is None:
            continue
        bundle_item = (
            current_claims[citation.index - 1] if citation.index <= len(current_claims) else {}
        )
        if not bundle_item:
            continue
        payload = citation_payload(citation.marker, bundle_item)
        claim_ref = _current_claim_ref(current_run_id, bundle_item)
        if claim_ref:
            payload["claim_ref"] = claim_ref
        citations[str(marker_index)] = payload
        next_marker = max(next_marker, marker_index + 1)

    historical_bundle = historical_claim_bundle(historical_claims)
    historical_by_ref = {
        item.get("claim_ref"): item for item in historical_bundle if item.get("claim_ref")
    }
    assigned_refs: dict[str, int] = {}
    for match in _HISTORICAL_ALIAS_MARKER_RE.finditer(answer):
        marker = match.group(0)
        alias = f"H{int(match.group(1))}"
        claim_ref = (historical_claim_alias_refs or {}).get(alias, "")
        if claim_ref:
            bundle_item = historical_by_ref.get(claim_ref)
        else:
            alias_index = int(match.group(1))
            bundle_item = (
                historical_bundle[alias_index - 1]
                if 1 <= alias_index <= len(historical_bundle)
                else {}
            )
        if not bundle_item:
            continue
        claim_ref = str(bundle_item.get("claim_ref") or "")
        if not claim_ref:
            continue
        display_index = assigned_refs.get(claim_ref)
        if display_index is None:
            display_index = next_marker
            next_marker += 1
            assigned_refs[claim_ref] = display_index
            payload = citation_payload(f"[{display_index}]", bundle_item)
            payload["claim_ref"] = claim_ref
            citations[str(display_index)] = payload
        final_answer = final_answer.replace(marker, f"[{display_index}]")
    for match in _STABLE_CLAIM_MARKER_RE.finditer(answer):
        marker = match.group(0)
        claim_ref = marker.strip("[]")
        bundle_item = historical_by_ref.get(claim_ref)
        if not bundle_item:
            continue
        display_index = assigned_refs.get(claim_ref)
        if display_index is None:
            display_index = next_marker
            next_marker += 1
            assigned_refs[claim_ref] = display_index
            payload = citation_payload(f"[{display_index}]", bundle_item)
            payload["claim_ref"] = claim_ref
            citations[str(display_index)] = payload
        final_answer = final_answer.replace(marker, f"[{display_index}]")
    prior_map = historical_message_citation_map or HistoricalMessageCitationMap()
    for match in re.finditer(r"\[(\d+)\]", final_answer):
        marker_index = match.group(1)
        if marker_index in citations:
            continue
        prior_citation = prior_map.citations.get(marker_index)
        if not isinstance(prior_citation, dict):
            continue
        claim_ref = str(prior_citation.get("claim_ref") or "")
        bundle_item = historical_by_ref.get(claim_ref)
        if not bundle_item:
            continue
        payload = citation_payload(f"[{marker_index}]", bundle_item)
        payload["claim_ref"] = claim_ref
        citations[marker_index] = payload
    final_answer = _remove_unresolved_bare_citation_markers(final_answer, citations)
    if not citations:
        fallback_item = _visual_ground_fallback_citation_item(current_claims)
        if fallback_item:
            marker = f"[{next_marker}]"
            payload = citation_payload(marker, fallback_item)
            claim_ref = _current_claim_ref(current_run_id, fallback_item)
            if claim_ref:
                payload["claim_ref"] = claim_ref
            citations[str(next_marker)] = payload
            final_answer = _append_terminal_citation(final_answer, marker)
    return final_answer, citations


def _visual_ground_fallback_citation_item(
    current_claims: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    for item in current_claims:
        if not isinstance(item, dict):
            continue
        if item.get("tool") != "visual_ground":
            continue
        source = item.get("source")
        if not isinstance(source, dict) or source.get("type") != "video":
            continue
        return item
    return {}


def _append_terminal_citation(answer: str, marker: str) -> str:
    stripped = answer.rstrip()
    if not stripped:
        return marker
    return f"{stripped}{marker}"


def _remove_unresolved_bare_citation_markers(
    answer: str,
    citations: dict[str, dict[str, Any]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in citations else ""

    return re.sub(r"\[(\d+)\]", replace, answer)


def _assign_completed_tool_payload_citations(
    state: _RunState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result = _assign_completed_tool_result_citations(
        state,
        str(payload.get("name") or ""),
        payload.get("result"),
        tool_call_id=str(payload.get("id") or ""),
    )
    if result is payload.get("result"):
        return payload
    payload = dict(payload)
    payload["result"] = result
    return payload


def _assign_completed_tool_result_citations(
    state: _RunState,
    tool_name: str,
    result: Any,
    *,
    tool_call_id: str = "",
) -> Any:
    if tool_name not in _INSPECT_TOOL_NAMES:
        return result
    return _normalize_claim_tool_result(
        state,
        tool_name,
        tool_call_id=tool_call_id,
        result=result,
    )


def _current_claim_ref(current_run_id: str | None, bundle_item: dict[str, Any]) -> str:
    if not current_run_id:
        return ""
    tool_call_id = str(bundle_item.get("tool_call_id") or "")
    claim_id = str(bundle_item.get("claim_id") or "")
    if tool_call_id and claim_id:
        return f"claim:{current_run_id}:{tool_call_id}:{claim_id}"
    citation_index = bundle_item.get("citation_index")
    if tool_call_id and isinstance(citation_index, int):
        return f"claim:{current_run_id}:{tool_call_id}:{citation_index}"
    return ""


def _append_evidence(evidence: list[Evidence], seen_ids: set[str], value: Any) -> None:
    if not isinstance(value, dict):
        return
    try:
        item = Evidence.model_validate(value)
    except ValueError:
        return
    if item.id in seen_ids:
        return
    seen_ids.add(item.id)
    evidence.append(item)


def _parse_json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _collect_claim_refs(value: Any) -> list[str]:
    """Collect stable claim references from a structured inspect result."""

    refs: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if item.startswith("claim:") and item not in refs:
                refs.append(item)
            return
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return refs


def _resolve_model_timeout_seconds(
    model_config: AgentModelConfig,
    profile: AgentProfile,
) -> float | None:
    if model_config.timeout_seconds is not None:
        return model_config.timeout_seconds
    budget = profile.runtime_budget
    if budget.enabled:
        return budget.time_limit_seconds
    return None


async def _yield_until_abort(stream: Any, abort: asyncio.Event | None) -> AsyncIterator[Any]:
    # Abort unblocks a hung anext so in-flight model.generate does not wait for the LLM.

    if not hasattr(stream, "__aiter__"):
        stream = await stream
    if abort is None:
        async for item in stream:
            yield item
        return
    iterator = stream.__aiter__()
    try:
        while not abort.is_set():
            next_item = asyncio.create_task(anext(iterator))
            abort_wait = asyncio.create_task(abort.wait())
            try:
                done, pending = await asyncio.wait(
                    {next_item, abort_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                next_item.cancel()
                abort_wait.cancel()
                await asyncio.gather(next_item, abort_wait, return_exceptions=True)
                raise
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if abort.is_set() or abort_wait in done:
                return
            try:
                yield next_item.result()
            except StopAsyncIteration:
                return
    finally:
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose) and abort.is_set():
            with suppress(Exception):
                await aclose()


async def _await_stream(stream: Any) -> AsyncIterator[dict[str, Any]]:
    async for event in _yield_until_abort(stream, _RUN_BUDGET_ABORT.get()):
        yield event


def _is_summarization_metadata(metadata: Any) -> bool:
    return isinstance(metadata, dict) and metadata.get("lc_source") == "summarization"


def _observe_agent_loop_span(spans: SpanSet, event: dict[str, Any], *, run_id: str) -> None:
    """Mirror LangChain model/tool events as children of ``agent.loop``.

    Scheduler spans can miss this tree when the model runs on a path that
    skips ``acquire`` or drops OTel context. These events are already
    consumed on the loop task.
    """

    event_name = event.get("event")
    langchain_run_id = str(event.get("run_id") or "")
    if event_name == "on_chat_model_start":
        if _is_summarization_metadata(event.get("metadata")):
            return
        spans.start(
            f"model:{langchain_run_id}",
            "model.generate",
            **{
                ATTR_PHASE: "model.generate",
                ATTR_RUN_ID: run_id,
                "agents.runtime.model": str(event.get("name") or ""),
            },
        )
        return
    if event_name in {"on_chat_model_end", "on_chat_model_error"}:
        spans.end(
            f"model:{langchain_run_id}",
            error_code="MODEL_GENERATE_FAILED" if event_name == "on_chat_model_error" else None,
        )
        return
    if event_name == "on_tool_start":
        spans.start(
            f"tool:{langchain_run_id}",
            "tool.execute",
            **{
                ATTR_PHASE: "tool.execute",
                ATTR_TOOL: str(event.get("name") or ""),
                ATTR_RUN_ID: run_id,
            },
        )
        return
    if event_name in {"on_tool_end", "on_tool_error"}:
        spans.end(
            f"tool:{langchain_run_id}",
            error_code="TOOL_FAILED" if event_name == "on_tool_error" else None,
        )


def _map_context_compaction_from_payload(
    value: Any,
) -> tuple[AgentEventType, dict[str, Any]] | None:
    event = _find_summarization_event(value)
    if event is None:
        return None
    return AgentEventType.CONTEXT_COMPACTED, {
        "cutoff_index": event.get("cutoff_index"),
        "summary": _extract_text(event.get("summary_message")),
        "file_path": event.get("file_path"),
    }


def _find_summarization_event(value: Any, *, depth: int = 0) -> dict[str, Any] | None:
    if value is None or depth > 8:
        return None
    update = getattr(value, "update", None)
    if isinstance(update, dict):
        found = _find_summarization_event(update, depth=depth + 1)
        if found is not None:
            return found
    if isinstance(value, dict):
        summarization_event = value.get("_summarization_event")
        if isinstance(summarization_event, dict):
            return summarization_event
        if {"cutoff_index", "summary_message", "file_path"}.issubset(value):
            return value
        for key in ("data", "chunk", "output", "update", "params"):
            found = _find_summarization_event(value.get(key), depth=depth + 1)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_summarization_event(item, depth=depth + 1)
            if found is not None:
                return found
    return None


def _tool_gate_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    gate = unwrap_tool_data(payload).get("tool_gate")
    return gate if isinstance(gate, dict) and gate.get("blocked") else None


def _context_compaction_key(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "cutoff_index": payload.get("cutoff_index"),
            "file_path": payload.get("file_path"),
            "summary": payload.get("summary"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _mapped_tool_call_id(state: _RunState, call_id: str) -> str:
    return state.tool_call_id_aliases.get(call_id, call_id)


def _canonicalize_answer_plan_claim_refs(
    state: _RunState,
    update: AnswerPlanUpdate,
) -> AnswerPlanUpdate:
    """Translate agent-visible tool-call ids to the ledger's canonical ids."""
    slot_updates = []
    for slot_update in update.slot_updates:
        claim_refs = [
            _canonical_claim_ref(state, claim_ref) for claim_ref in slot_update.inspect_claim_refs
        ]
        verified_items = [
            item.model_copy(
                update={
                    "inspect_claim_refs": [
                        _canonical_claim_ref(state, claim_ref)
                        for claim_ref in item.inspect_claim_refs
                    ]
                }
            )
            for item in slot_update.verified_items
        ]
        slot_updates.append(
            slot_update.model_copy(
                update={
                    "inspect_claim_refs": claim_refs,
                    "verified_items": verified_items,
                }
            )
        )
    return update.model_copy(update={"slot_updates": slot_updates})


def _evidence_locator_catalog_segment_ids(
    locator: EvidenceLocatorFact | None,
    plan: RunAnswerPlan,
) -> set[tuple[str, str]]:
    if locator is None:
        return set()
    library_id = str(getattr(locator, "library_id", "") or "")
    asset_id = str(getattr(locator, "asset_id", "") or "")
    if not library_id or not asset_id:
        return set()
    scoped_segments = {
        (coverage.catalog_id, segment.segment_id)
        for coverage in plan.catalog_coverages
        if coverage.library_id == library_id and coverage.asset_id == asset_id
        for segment in coverage.segments
    }
    direct_ids = {
        str(value)
        for value in (
            getattr(locator, "evidence_id", ""),
            getattr(locator, "segment_id", ""),
        )
        if value
    }
    matched_ids = {identity for identity in scoped_segments if identity[1] in direct_ids}
    if matched_ids:
        return matched_ids
    time_range = getattr(locator, "time_range", None)
    if not isinstance(time_range, dict):
        return set()
    start = time_range.get("start_seconds")
    end = time_range.get("end_seconds")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return set()
    return {
        (coverage.catalog_id, segment.segment_id)
        for coverage in plan.catalog_coverages
        if coverage.library_id == library_id and coverage.asset_id == asset_id
        for segment in coverage.segments
        if segment.time_start is not None
        and segment.time_end is not None
        and segment.time_start < float(end)
        and segment.time_end > float(start)
    }


def _canonical_claim_ref(state: _RunState, claim_ref: str) -> str:
    parts = claim_ref.split(":")
    if len(parts) < 4 or parts[0] != "claim":
        return claim_ref
    tool_call_id = ":".join(parts[2:-1])
    canonical_id = _mapped_tool_call_id(state, tool_call_id)
    if canonical_id == tool_call_id:
        return claim_ref
    return ":".join((parts[0], parts[1], canonical_id, parts[-1]))


def _remember_tool_call_id_alias(state: _RunState, *, alternate_id: str, canonical_id: str) -> None:
    """Map an alternate framework id onto the UI/canonical tool-call id."""
    if not alternate_id or not canonical_id or alternate_id == canonical_id:
        return
    state.tool_call_id_aliases[alternate_id] = canonical_id


def _canonical_tool_call_event_id(state: _RunState, trace_run_id: str, raw_call_id: str) -> str:
    """Public completed/failed id must match the TOOL_CALL_STARTED card id.

    LangGraph tool spans use ``run_id`` (e.g. ``019fea28-...``) on ``on_tool_start``,
    while ``on_tool_end`` / final messages often carry the model ``call_...`` id.
    Prefer the already-started id so the web UI can close the running card without
    depending on a best-effort ``stream_id`` bridge.
    """
    raw = str(raw_call_id or "").strip()
    trace = str(trace_run_id or "").strip()

    if trace and trace in state.started_tool_call_ids:
        _remember_tool_call_id_alias(state, alternate_id=raw, canonical_id=trace)
        return trace
    if raw and raw in state.started_tool_call_ids:
        _remember_tool_call_id_alias(state, alternate_id=trace, canonical_id=raw)
        return raw

    for candidate in (raw, trace):
        mapped = state.tool_call_id_aliases.get(candidate, "")
        if mapped and mapped in state.started_tool_call_ids:
            _remember_tool_call_id_alias(state, alternate_id=raw, canonical_id=mapped)
            _remember_tool_call_id_alias(state, alternate_id=trace, canonical_id=mapped)
            return mapped

    # No matching start yet (final-only path): keep the model/tool id, and remember
    # the span id as an alternate so a late start can still resolve.
    if raw:
        _remember_tool_call_id_alias(state, alternate_id=trace, canonical_id=raw)
        return raw
    return trace


def _stream_tool_call_event_id(state: _RunState, trace_run_id: str, call_id: str) -> str:
    """Backward-compatible name for completed/failed id canonicalization."""
    return _canonical_tool_call_event_id(state, trace_run_id, call_id)


def _thread_id(user_id: str, workspace_id: str | None, session_id: str) -> str:
    return f"{user_id}:{workspace_id or 'default'}:{session_id}"


def deepagents_session_workspace_dir(
    config: DeepAgentsKernelConfig,
    *,
    user_id: str,
    workspace_id: str | None,
    session_id: str,
) -> Path:
    """Return the host workspace directory for one DeepAgents session."""

    if config.workspace.root_dir is None:
        raise RuntimeError("deepagents workspace.root_dir is required")
    return config.workspace.root_dir / _safe_thread_path(
        _thread_id(user_id, workspace_id, session_id)
    )


def _split_provider_qualified_model(value: str) -> tuple[str, str] | None:
    provider_name, separator, model = value.partition(":")
    if not separator:
        return None
    provider_name = provider_name.strip()
    model = model.strip()
    if not provider_name or not model:
        return None
    return provider_name, model


def _agent_model_config_from_tool_model(config: ToolModelConfig) -> AgentModelConfig:
    return AgentModelConfig(
        ref=config.ref,
        provider=config.provider,
        model=config.model,
        model_env=config.model_env,
        base_url=config.base_url,
        base_url_env=config.base_url_env,
        api_key_env=config.api_key_env,
        reasoning_content_roundtrip=config.reasoning_content_roundtrip,
        temperature=0,
        timeout_seconds=config.timeout_seconds,
        gateway=config.gateway,
    )


def _safe_thread_path(thread_id: str) -> str:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
    return f"{thread_id.replace('/', '_').replace(':', '_')}-{digest}"


def _subagent_skill_source_paths(
    skill_source: str | None,
    subagent_skills: dict[str, list[SkillSpec]],
) -> dict[str, str]:
    if skill_source is None:
        return {}
    return {
        name: f"{skill_source}/subagents/{name}"
        for name, skills in subagent_skills.items()
        if skills
    }


def _stage_runtime_skills(
    workspace_dir: Path,
    skills: list[SkillSpec],
    subagent_skills: dict[str, list[SkillSpec]] | None = None,
) -> bool:
    target_root = workspace_dir / _RUNTIME_SKILLS_DIR
    if target_root.exists():
        shutil.rmtree(target_root)
    if not skills and not subagent_skills:
        return False
    target_root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        _stage_runtime_skill(skill, target_root / skill.name)
    for subagent_name, role_skills in (subagent_skills or {}).items():
        role_root = target_root / "subagents" / subagent_name
        for skill in role_skills:
            _stage_runtime_skill(skill, role_root / skill.name)
    return True


def _stage_runtime_skill(skill: SkillSpec, target_dir: Path) -> None:
    shutil.copytree(
        skill.source_dir,
        target_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _remove_runtime_only_skill_metadata(target_dir / "SKILL.md")


def _remove_runtime_only_skill_metadata(skill_file: Path) -> None:
    """Hide ViKnow validation-only metadata from DeepAgents' skill prompt."""

    content = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if match is None:
        return
    frontmatter = yaml.safe_load(match.group(1))
    if not isinstance(frontmatter, dict) or "allowed-tools" not in frontmatter:
        return
    sanitized = dict(frontmatter)
    sanitized.pop("allowed-tools", None)
    rendered_frontmatter = yaml.safe_dump(
        sanitized,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    skill_file.write_text(
        f"---\n{rendered_frontmatter}\n---\n{content[match.end() :]}",
        encoding="utf-8",
    )


def _workspace_uploads(
    workspace_dir: Path,
    *,
    prefixes: Sequence[str] | None = None,
) -> list[tuple[str, bytes]]:
    if not workspace_dir.exists():
        return []
    uploads: list[tuple[str, bytes]] = []
    roots = (
        [workspace_dir / prefix for prefix in prefixes] if prefixes is not None else [workspace_dir]
    )
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or _is_excluded_workspace_path(path):
                continue
            relative = path.relative_to(workspace_dir).as_posix()
            uploads.append((f"{_WORKSPACE_ROOT}/{relative}", path.read_bytes()))
    return uploads


def _runtime_skill_uploads(workspace_dir: Path) -> list[tuple[str, bytes]]:
    runtime_skill_root = workspace_dir / _RUNTIME_SKILLS_DIR
    if not runtime_skill_root.exists():
        return []
    uploads: list[tuple[str, bytes]] = []
    for path in sorted(runtime_skill_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace_dir).as_posix()
        uploads.append((f"{_WORKSPACE_ROOT}/{relative}", path.read_bytes()))
    return uploads


def _list_workspace_paths(
    backend: Any,
    *,
    prefixes: Sequence[str] | None = None,
) -> list[str]:
    glob = getattr(backend, "glob", None)
    if not callable(glob):
        raise RuntimeError("bubblewrap sandbox backend does not support glob")
    search_roots = (
        [f"{_WORKSPACE_ROOT}/{prefix}" for prefix in prefixes]
        if prefixes is not None
        else [_WORKSPACE_ROOT]
    )
    paths: list[str] = []
    for root in search_roots:
        result = glob("**/*", path=root)
        if getattr(result, "error", None):
            # Missing outputs/scratch is fine; treat as empty.
            continue
        for match in getattr(result, "matches", None) or []:
            if not isinstance(match, dict) or match.get("is_dir"):
                continue
            path = _workspace_match_path(match.get("path"))
            if path is None or _is_excluded_workspace_path(Path(path)):
                continue
            if prefixes is not None and not _path_has_prefix(path, prefixes):
                continue
            paths.append(path)
    return sorted(set(paths))


def _path_has_prefix(sandbox_path: str, prefixes: Sequence[str]) -> bool:
    relative = _workspace_relative_path(sandbox_path)
    if relative is None or not relative.parts:
        return False
    return relative.parts[0] in prefixes


def _replace_host_workspace(workspace_dir: Path, files: list[tuple[str, bytes]]) -> None:
    """Legacy full replace — kept for tests that still call it directly."""

    prepared: list[tuple[Path, bytes]] = []
    for sandbox_path, content in files:
        relative = _workspace_relative_path(sandbox_path)
        if relative is None:
            continue
        prepared.append((relative, content))
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    for relative, content in prepared:
        target = workspace_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _merge_host_workspace(
    workspace_dir: Path,
    files: list[tuple[str, bytes]],
    *,
    prefixes: Sequence[str],
) -> None:
    """Merge captured files into host staging without touching uploads."""

    for prefix in prefixes:
        target_root = workspace_dir / prefix
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
    for sandbox_path, content in files:
        relative = _workspace_relative_path(sandbox_path)
        if relative is None or not relative.parts or relative.parts[0] not in prefixes:
            continue
        target = workspace_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _workspace_match_path(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    if path.startswith("/"):
        return path
    return f"{_WORKSPACE_ROOT}/{path.lstrip('/')}"


def _workspace_relative_path(sandbox_path: str) -> Path | None:
    if not sandbox_path.startswith(f"{_WORKSPACE_ROOT}/"):
        return None
    relative = PurePosixPath(sandbox_path.removeprefix(f"{_WORKSPACE_ROOT}/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return Path(*relative.parts)


def _is_excluded_workspace_path(path: Path) -> bool:
    return any(part in _EXCLUDED_WORKSPACE_DIRS for part in path.parts)


def _batch(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _with_artifacts_root(backend: Any, artifacts_root: str) -> Any:
    from deepagents.backends.composite import CompositeBackend

    return CompositeBackend(default=backend, routes={}, artifacts_root=artifacts_root)


def _prepare_bubblewrap_workspace_access(
    backend: Any,
    paths: Sequence[str] | None = None,
) -> None:
    root = _native_workspace_root(backend)
    if root is None:
        return
    for target in _workspace_access_targets(root, paths):
        _make_accessible_to_sandbox_user(target)


def _native_workspace_root(backend: Any) -> Path | None:
    native_sandbox = getattr(backend, "_sandbox", None)
    workspace = getattr(native_sandbox, "workspace", None)
    root = getattr(workspace, "root", None)
    if not isinstance(root, str) or not root:
        return None
    return Path(root).resolve()


def _workspace_access_targets(root: Path, paths: Sequence[str] | None) -> list[Path]:
    if paths is None:
        return [root]
    targets = {root}
    for sandbox_path in paths:
        relative = _workspace_relative_path(sandbox_path)
        if relative is None:
            continue
        host_path = (root / relative).resolve()
        try:
            if os.path.commonpath([root, host_path]) != str(root):
                continue
        except ValueError:
            continue
        targets.add(host_path)
        for parent in host_path.parents:
            targets.add(parent)
            if parent == root:
                break
    return sorted(targets, key=lambda path: len(path.parts))


def _make_accessible_to_sandbox_user(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.geteuid() == 0:
            os.chown(path, _SANDBOX_UID, _SANDBOX_GID)
        required_mode = 0o777 if path.is_dir() else 0o666
        current_mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, current_mode | required_mode)
    except OSError as exc:
        raise RuntimeError(
            f"failed to prepare bubblewrap workspace permissions for {path}"
        ) from exc


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if content is not None:
        return _extract_text(content)
    content_blocks = getattr(value, "content_blocks", None)
    if content_blocks is not None:
        return _extract_text(content_blocks)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif _is_reasoning_block(item):
                    continue
            else:
                parts.append(_extract_text(item))
        return "".join(parts)
    if isinstance(value, dict):
        if _is_reasoning_block(value):
            return ""
        if value.get("type") == "text":
            return str(value.get("text") or "")
        content = value.get("content")
        if content is not None:
            return _extract_text(content)
    return str(value)


def _extract_reasoning(value: Any) -> str:
    if value is None or isinstance(value, str):
        return ""
    for attr_name in ("reasoning_content", "reasoning"):
        reasoning = _extract_reasoning_payload(getattr(value, attr_name, None))
        if reasoning:
            return reasoning
    for attr_name in ("additional_kwargs", "response_metadata"):
        attr_value = getattr(value, attr_name, None)
        reasoning = _extract_reasoning(attr_value)
        if reasoning:
            return reasoning
    content_blocks = getattr(value, "content_blocks", None)
    reasoning = _extract_reasoning_payload(content_blocks)
    if reasoning:
        return reasoning
    content = getattr(value, "content", None)
    if content is not None and not isinstance(content, str):
        reasoning = _extract_reasoning_payload(content)
        if reasoning:
            return reasoning
    if isinstance(value, dict):
        for key in ("reasoning_content", "reasoning", "reasoning_summary"):
            reasoning = _extract_reasoning_payload(value.get(key))
            if reasoning:
                return reasoning
        for key in ("additional_kwargs", "response_metadata", "message", "delta"):
            reasoning = _extract_reasoning(value.get(key))
            if reasoning:
                return reasoning
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                reasoning = _extract_reasoning(choice)
                if reasoning:
                    return reasoning
        output = value.get("output")
        reasoning = _extract_reasoning_payload(output)
        if reasoning:
            return reasoning
        content = value.get("content")
        if content is not None and not isinstance(content, str):
            reasoning = _extract_reasoning_payload(content)
            if reasoning:
                return reasoning
    return ""


def _extract_reasoning_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if _is_reasoning_block(value):
            for key in ("text", "reasoning", "reasoning_content", "content"):
                text = _extract_reasoning_payload(value.get(key))
                if text:
                    return text
            summary = _extract_reasoning_payload(value.get("summary"))
            if summary:
                return summary
        if value.get("type") in {"summary_text", "reasoning_summary_text"}:
            return str(value.get("text") or "")
        return _extract_reasoning(value)
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            text = _extract_reasoning_payload(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return _extract_reasoning(value)


def _is_reasoning_block(value: dict[str, Any]) -> bool:
    block_type = str(value.get("type") or "")
    return block_type in {
        "reasoning",
        "reasoning_content",
        "reasoning_delta",
        "reasoning_summary",
        "reasoning_summary_text",
        "summary_text",
    }


def _messages_after_last_user_message(messages: list[Any]) -> list[Any]:
    for index in range(len(messages) - 1, -1, -1):
        if _is_user_message(messages[index]):
            return messages[index + 1 :]
    return messages


def _select_final_answer_text(messages: list[Any]) -> str:
    """Pick the user-facing final answer from assistant turns after the last user message.

    Prefers the last citation-bearing answer over a later empty summary restatement.
    """
    candidates = _final_answer_candidate_texts(_messages_after_last_user_message(messages))
    if not candidates:
        return ""

    cited = [text for text in candidates if _answer_has_citation_markers(text)]
    if cited:
        return cited[-1]
    return candidates[-1]


def _final_answer_candidate_texts(messages: list[Any]) -> list[str]:
    candidates: list[str] = []
    for message in messages:
        if not _is_assistant_message(message):
            continue
        if _message_tool_calls(message):
            continue
        text = _extract_text(_message_content(message)).strip()
        if text:
            candidates.append(text)
    return candidates


def _answer_has_citation_markers(text: str) -> bool:
    return bool(_ANSWER_CITATION_MARKER_RE.search(text))


def _is_user_message(message: Any) -> bool:
    role = getattr(message, "role", None)
    message_type = getattr(message, "type", None)
    if isinstance(message, dict):
        role = message.get("role", role)
        message_type = message.get("type", message_type)
    return str(role or message_type or "").lower() in {"user", "human"}


def _message_tool_calls(message: Any) -> Any:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is not None:
        return tool_calls
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return []


def _message_tool_call_id(message: Any) -> str:
    call_id = getattr(message, "tool_call_id", None)
    if call_id is None and isinstance(message, dict):
        call_id = message.get("tool_call_id")
    return str(call_id or "")


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_tool_result(message: Any) -> str:
    full_result = _tool_result_full_result(message)
    if full_result is not None:
        return full_result
    nested_message = _first_nested_tool_message(message)
    if nested_message is not None:
        return _message_tool_result(nested_message)
    if isinstance(message, ToolMessage):
        return _serialize_tool_message_content(message)
    return _serialize(_message_content(message))


def _tool_result_full_result(value: Any) -> str | None:
    artifact = _tool_result_artifact(value)
    if not artifact:
        nested_message = _first_nested_tool_message(value)
        if nested_message is not None:
            artifact = _tool_result_artifact(nested_message)
    full_result = artifact.get("full_result")
    if full_result is None:
        return None
    return str(full_result)


def _tool_result_artifact(value: Any) -> dict[str, Any]:
    artifact = getattr(value, "artifact", None)
    if artifact is None and isinstance(value, dict):
        artifact = value.get("artifact")
    if not isinstance(artifact, dict):
        return {}
    return artifact


def _event_tool_call_id(
    event: dict[str, Any],
    data: dict[str, Any],
    metadata: Any,
) -> str:
    for candidate in (
        data.get("tool_call_id"),
        event.get("tool_call_id"),
        _tool_call_payload_id(data.get("input")),
        _tool_message_call_id(data.get("output")),
        metadata.get("tool_call_id") if isinstance(metadata, dict) else None,
        _tool_call_payload_id(metadata.get("input")) if isinstance(metadata, dict) else None,
    ):
        if candidate:
            return str(candidate)
    return str(event.get("run_id") or "")


def _tool_event_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict) and _is_tool_call_payload(arguments):
        return arguments.get("args")
    return arguments


def _tool_call_payload_id(value: Any) -> str:
    if not isinstance(value, dict) or not _is_tool_call_payload(value):
        return ""
    return str(value.get("id") or value.get("call_id") or "")


def _is_tool_call_payload(value: dict[str, Any]) -> bool:
    if value.get("type") == "tool_call":
        return True
    return "args" in value and "name" in value and ("id" in value or "call_id" in value)


def _tool_message_call_id(value: Any) -> str:
    call_id = _message_tool_call_id(value)
    if call_id:
        return call_id
    if isinstance(value, (list, tuple)):
        for item in value:
            call_id = _tool_message_call_id(item)
            if call_id:
                return call_id
    nested_message = _first_nested_tool_message(value)
    if nested_message is not None and nested_message is not value:
        return _tool_message_call_id(nested_message)
    return ""


def _first_nested_tool_message(value: Any) -> Any | None:
    if isinstance(value, Command):
        value = value.update
    if not isinstance(value, dict):
        return None
    messages = value.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None
    for message in messages:
        if _message_tool_call_id(message):
            return message
    return None


def _serialize_raw_model_output(value: Any) -> str:
    text = json.dumps(_raw_model_output_value(value), ensure_ascii=False, default=str)
    if len(text) <= _MAX_RAW_MODEL_OUTPUT_CHARS:
        return text
    return json.dumps(
        {
            "truncated": True,
            "char_limit": _MAX_RAW_MODEL_OUTPUT_CHARS,
            "payload_prefix": text[:_MAX_RAW_MODEL_OUTPUT_CHARS],
        },
        ensure_ascii=False,
    )


def _raw_model_output_value(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_RAW_MODEL_OUTPUT_DEPTH:
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _raw_model_output_value(item, depth=depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_raw_model_output_value(item, depth=depth + 1) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _raw_model_output_value(model_dump(mode="json"), depth=depth + 1)
        except TypeError:
            try:
                return _raw_model_output_value(model_dump(), depth=depth + 1)
            except TypeError:
                pass

    fields: dict[str, Any] = {}
    for attr_name in (
        "type",
        "content",
        "tool_calls",
        "invalid_tool_calls",
        "tool_call_id",
        "name",
        "id",
        "additional_kwargs",
        "response_metadata",
        "usage_metadata",
    ):
        if hasattr(value, attr_name):
            fields[attr_name] = _raw_model_output_value(getattr(value, attr_name), depth=depth + 1)
    if fields:
        return fields
    return str(value)


def _extract_token_usage(value: Any) -> dict[str, Any]:
    usage = _find_token_usage(value)
    if usage is None:
        return {}
    return _normalize_token_usage(usage)


def _resolve_model_call_usage(
    *,
    event: dict[str, Any],
    state: _RunState,
    event_token_usage: dict[str, Any],
    output: Any,
) -> dict[str, Any] | None:
    trace_run_id = str(event.get("run_id") or "__unknown__")
    for candidate in (
        event_token_usage,
        _extract_token_usage(output),
        state.model_token_usages.get(trace_run_id, {}),
    ):
        normalized = _normalize_token_usage(candidate)
        if normalized:
            return normalized
    return None


def _find_token_usage(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_RAW_MODEL_OUTPUT_DEPTH or value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return None

    usage_metadata = getattr(value, "usage_metadata", None)
    if isinstance(usage_metadata, dict) and usage_metadata:
        return usage_metadata
    metadata_usage = _metadata_token_usage(getattr(value, "response_metadata", None))
    if metadata_usage is not None:
        return metadata_usage
    message = getattr(value, "message", None)
    if message is not None:
        found = _find_token_usage(message, depth=depth + 1)
        if found is not None:
            return found
    generation_info = getattr(value, "generation_info", None)
    if generation_info is not None:
        found = _find_token_usage(generation_info, depth=depth + 1)
        if found is not None:
            return found
    generations = getattr(value, "generations", None)
    if isinstance(generations, list):
        for generation in reversed(generations):
            found = _find_token_usage(generation, depth=depth + 1)
            if found is not None:
                return found

    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                found = _find_token_usage(message, depth=depth + 1)
                if found is not None:
                    return found
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in reversed(choices):
                found = _find_token_usage(choice, depth=depth + 1)
                if found is not None:
                    return found
        for key in ("usage_metadata", "token_usage", "usage"):
            candidate = value.get(key)
            if isinstance(candidate, dict) and candidate:
                return candidate
        metadata_usage = _metadata_token_usage(value.get("response_metadata"))
        if metadata_usage is not None:
            return metadata_usage
        for key in ("message", "delta", "output", "data", "chunk", "result"):
            found = _find_token_usage(value.get(key), depth=depth + 1)
            if found is not None:
                return found

    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            found = _find_token_usage(item, depth=depth + 1)
            if found is not None:
                return found
    return None


def _metadata_token_usage(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("usage_metadata", "token_usage", "usage"):
        candidate = value.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def _normalize_token_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    input_tokens = _token_count(value.get("input_tokens"), value.get("prompt_tokens"))
    output_tokens = _token_count(value.get("output_tokens"), value.get("completion_tokens"))
    total_tokens = _token_count(value.get("total_tokens"))
    if input_tokens is not None:
        normalized["input_tokens"] = input_tokens
    if output_tokens is not None:
        normalized["output_tokens"] = output_tokens
    if total_tokens is not None:
        normalized["total_tokens"] = total_tokens

    input_details = _normalize_token_details(
        value.get("input_token_details") or value.get("prompt_tokens_details")
    )
    output_details = _normalize_token_details(
        value.get("output_token_details") or value.get("completion_tokens_details")
    )
    if input_details:
        normalized["input_token_details"] = input_details
    if output_details:
        normalized["output_token_details"] = output_details
    return normalized


def _aggregate_token_usages(values: Iterable[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    input_details: dict[str, int] = {}
    output_details: dict[str, int] = {}
    for value in values:
        usage = _normalize_token_usage(value)
        if not usage:
            continue
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            count = _token_count(usage.get(key))
            if count is not None:
                aggregate[key] = int(aggregate.get(key) or 0) + count
        _merge_token_details(input_details, usage.get("input_token_details"))
        _merge_token_details(output_details, usage.get("output_token_details"))
    if input_details:
        aggregate["input_token_details"] = input_details
    if output_details:
        aggregate["output_token_details"] = output_details
    return aggregate


def _token_count(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdecimal():
                return int(stripped)
    return None


def _normalize_token_details(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    details: dict[str, int] = {}
    for key, item in value.items():
        count = _token_count(item)
        if count is not None:
            details[str(key)] = count
    return details


def _merge_token_details(target: dict[str, int], value: Any) -> None:
    for key, count in _normalize_token_details(value).items():
        target[key] = target.get(key, 0) + count


def _serialize(value: Any) -> str:
    if isinstance(value, Command):
        message = _first_nested_tool_message(value)
        if message is None:
            raise ValueError("A tool Command must contain a result message for event projection")
        return _serialize(message)
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if content is not None:
        return _serialize(content)
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)
