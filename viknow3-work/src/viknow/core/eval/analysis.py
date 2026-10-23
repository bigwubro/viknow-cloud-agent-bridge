"""Content-bounded inputs and outputs for offline Skill Flow diagnosis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow.core.eval.quality_score import QualityScorecard

AnalysisCategory = Literal[
    "skill",
    "harness",
    "tool_or_index",
    "capability_gap",
    "evidence_insufficient",
]
RecommendedAction = Literal[
    "start_skillopt",
    "continue_observing",
    "route_harness",
    "route_knowledge_capability",
]
AnalysisCompleteness = Literal["events_only", "langfuse_enhanced"]
AnswerCorrectness = Literal["supported", "unsupported", "indeterminate"]


class RunAssessmentFact(BaseModel):
    """One deterministic fact projected from the source run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    event_sequences: list[int] = Field(default_factory=list)


class TraceReference(BaseModel):
    """A source-bound trace identity and the observations inspected for this analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    observation_ids: list[str] = Field(default_factory=list)


class RunAssessmentPackage(BaseModel):
    """Immutable deterministic context supplied to the self-learning analyst."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["viknow.run-assessment.v1"] = "viknow.run-assessment.v1"
    run_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str | None = None
    evaluation_stage: Literal["provisional", "accepted"]
    deterministic_result: dict[str, Any]
    facts: list[RunAssessmentFact] = Field(default_factory=list)
    trace_references: list[TraceReference] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)

    @property
    def known_fact_ids(self) -> set[str]:
        return {fact.fact_id for fact in self.facts}

    @property
    def known_observation_ids(self) -> set[str]:
        return {
            observation_id
            for reference in self.trace_references
            for observation_id in reference.observation_ids
        }


class RunAnalysisObservation(BaseModel):
    """A semantic observation backed by deterministic or ClickHouse trace facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1)
    fact_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_source_reference(self) -> RunAnalysisObservation:
        if not self.fact_ids and not self.observation_ids:
            raise ValueError("analysis observation requires a fact or observation reference")
        return self


class RunAnalysisHypothesis(RunAnalysisObservation):
    category: AnalysisCategory
    confidence: float = Field(ge=0.0, le=1.0)


class RunAnalysisResult(BaseModel):
    """Auditable output produced by the dedicated self-learning analyst profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["viknow.run-analysis.v1"] = "viknow.run-analysis.v1"
    analysis_completeness: AnalysisCompleteness
    answer_correctness: AnswerCorrectness = Field(
        default="indeterminate",
        description=("相对已接受参考答案的事实正确性；临时评估必须为 indeterminate。"),
    )
    summary: str = Field(min_length=1)
    observations: list[RunAnalysisObservation]
    hypotheses: list[RunAnalysisHypothesis]
    primary_category: AnalysisCategory
    contributing_categories: list[AnalysisCategory] = Field(default_factory=list)
    optimization_target: str = Field(min_length=1)
    optimization_suggestion: str = Field(min_length=1)
    regression_risks: list[str]
    recommended_action: RecommendedAction
    requires_oracle: bool
    confidence: float = Field(ge=0.0, le=1.0)


def validate_run_analysis_result(
    package: RunAssessmentPackage,
    result: RunAnalysisResult,
    *,
    inspected_observation_ids: Iterable[str] = (),
    inspected_trace_fact_ids: Iterable[str] = (),
) -> None:
    """Reject claims that cite facts outside the bound run and inspected trace."""

    if package.evaluation_stage == "provisional" and result.answer_correctness != "indeterminate":
        raise ValueError("provisional analysis must leave answer correctness indeterminate")

    observations = [RunAnalysisObservation.model_validate(item) for item in result.observations]
    hypotheses = [RunAnalysisHypothesis.model_validate(item) for item in result.hypotheses]
    trace_fact_ids = set(inspected_trace_fact_ids)
    if result.analysis_completeness == "events_only" and any(
        item.observation_ids or trace_fact_ids.intersection(item.fact_ids)
        for item in [*observations, *hypotheses]
    ):
        raise ValueError("events_only analysis cannot cite observations or trace facts")
    for item in [*observations, *hypotheses]:
        unknown_fact_ids = set(item.fact_ids).difference(package.known_fact_ids | trace_fact_ids)
        if unknown_fact_ids:
            raise ValueError(f"analysis result cites unknown fact id: {min(unknown_fact_ids)}")
        known_observation_ids = package.known_observation_ids | set(inspected_observation_ids)
        unknown_observation_ids = set(item.observation_ids).difference(known_observation_ids)
        if unknown_observation_ids:
            raise ValueError(
                f"analysis result cites unknown observation id: {min(unknown_observation_ids)}"
            )


class SkillFlowAnalysisCase(BaseModel):
    """Non-content facts from one evaluated case that an analysis model may inspect."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    status: str
    verdict: str | None = None
    quality_optimization_score: float | None = Field(default=None, ge=0.0, le=100.0)
    failure_buckets: list[str] = Field(default_factory=list)
    harness_warning_signal_count: int | None = Field(default=None, ge=0)
    stop_gate_blocked: bool | None = None
    stop_gate_reason_code: str | None = None
    fact_ids: list[str] = Field(default_factory=list)


class SkillFlowAnalysisRequest(BaseModel):
    """A provisional, content-bounded bundle suitable for an offline analysis model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skill-flow-analysis-input.v1"] = (
        "viknow.skill-flow-analysis-input.v1"
    )
    evaluation_stage: Literal["provisional", "accepted"] = "provisional"
    cases: list[SkillFlowAnalysisCase] = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)


class AnalysisObservation(BaseModel):
    """A model observation that must be backed by one or more input facts."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)


class AnalysisHypothesis(AnalysisObservation):
    """A tentative diagnosis, never a replacement for the captured facts."""

    category: AnalysisCategory
    confidence: float = Field(ge=0.0, le=1.0)


class ReferenceFlowDraft(BaseModel):
    """Suggested process constraints, explicitly non-executable and provisional."""

    model_config = ConfigDict(extra="forbid")

    must: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)


class SkillFlowAnalysisResult(BaseModel):
    """Structured diagnostic output; callers must validate its fact references."""

    model_config = ConfigDict(extra="forbid")

    observations: list[AnalysisObservation]
    common_issues: list[str]
    hypotheses: list[AnalysisHypothesis]
    reference_flow_draft: ReferenceFlowDraft
    regression_risks: list[str]
    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)


class SkillFlowAnalysisArtifact(BaseModel):
    """A provisional analysis result bound to the exact facts supplied to its judge."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["viknow.skill-flow-analysis-result.v1"] = (
        "viknow.skill-flow-analysis-result.v1"
    )
    evaluation_stage: Literal["provisional", "accepted"]
    release_eligible: Literal[False] = False
    analysis_model: str
    input_sha256: str
    result: SkillFlowAnalysisResult


class SkillFlowAnalysisJudge(Protocol):
    """Explicit offline analysis port; implementations cannot change source facts."""

    model: str

    def analyze(self, request: SkillFlowAnalysisRequest) -> SkillFlowAnalysisResult: ...


class ConfiguredSkillFlowAnalysisJudge:
    """Structured LLM analysis, invoked only by an explicit offline caller."""

    def __init__(self, *, chat_model: Any, model: str, max_tokens: int) -> None:
        self._chat_model = chat_model
        self.model = model
        self._max_tokens = max_tokens

    def analyze(self, request: SkillFlowAnalysisRequest) -> SkillFlowAnalysisResult:
        from viknow.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self._chat_model,
            SkillFlowAnalysisResult,
            _analysis_prompt(request),
            run_name="skill_flow_analysis",
            max_attempts=1,
            max_tokens=self._max_tokens,
        )
        if result is None:
            raise ValueError("Skill Flow analysis judge returned no valid structured result")
        validate_skill_flow_analysis_result(request, result)
        return result


def build_skill_flow_analysis_judge(judge_model: Any) -> ConfiguredSkillFlowAnalysisJudge:
    """Build the offline analysis judge without adding a runtime dependency."""

    from viknow.core.config import ModelProvider

    if judge_model.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported Skill Flow analysis provider: {judge_model.provider}")
    base_url = judge_model.resolve_base_url()
    if not base_url:
        raise RuntimeError("Skill Flow analysis base_url or base_url_env is required")
    from langchain_openai import ChatOpenAI

    return ConfiguredSkillFlowAnalysisJudge(
        chat_model=ChatOpenAI(
            model=judge_model.require_model(),
            base_url=base_url,
            api_key=judge_model.require_api_key(),
            temperature=judge_model.temperature,
            timeout=judge_model.timeout_seconds,
            max_retries=0,
        ),
        model=judge_model.require_model(),
        max_tokens=judge_model.max_tokens,
    )


def build_skill_flow_analysis_request(
    runs: Iterable[Mapping[str, Any]],
    *,
    evaluation_stage: Literal["provisional", "accepted"] = "provisional",
) -> SkillFlowAnalysisRequest:
    """Extract only scored and deterministic diagnostic facts from run payloads."""

    cases = [_analysis_case_from_run(run) for run in runs]
    fact_ids = [fact_id for case in cases for fact_id in case.fact_ids]
    return SkillFlowAnalysisRequest(
        evaluation_stage=evaluation_stage,
        cases=cases,
        fact_ids=fact_ids,
    )


def validate_skill_flow_analysis_result(
    request: SkillFlowAnalysisRequest,
    result: SkillFlowAnalysisResult,
) -> None:
    """Reject diagnostic claims that cannot be traced to a supplied run fact."""

    known_fact_ids = set(request.fact_ids)
    for item in [*result.observations, *result.hypotheses]:
        unknown_fact_ids = set(item.fact_ids).difference(known_fact_ids)
        if unknown_fact_ids:
            unknown_fact_id = sorted(unknown_fact_ids)[0]
            raise ValueError(f"analysis result cites unknown fact id: {unknown_fact_id}")


def build_skill_flow_analysis_artifact(
    *,
    request: SkillFlowAnalysisRequest,
    result: SkillFlowAnalysisResult,
    analysis_model: str,
) -> SkillFlowAnalysisArtifact:
    """Bind an already validated analysis to its exact content-bounded input."""

    validate_skill_flow_analysis_result(request, result)
    input_bytes = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SkillFlowAnalysisArtifact(
        evaluation_stage=request.evaluation_stage,
        analysis_model=analysis_model,
        input_sha256=hashlib.sha256(input_bytes).hexdigest(),
        result=result,
    )


def _analysis_case_from_run(run: Mapping[str, Any]) -> SkillFlowAnalysisCase:
    case_id = str(run.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("analysis input run is missing case_id")
    status = str(run.get("status") or "unknown").strip() or "unknown"
    fact_ids = [f"{case_id}:status"]
    verdict = _nested_text(run, "verdict_result", "verdict")
    if verdict:
        fact_ids.append(f"{case_id}:verdict")
    score = _quality_score(run)
    if score is not None:
        fact_ids.append(f"{case_id}:quality_scorecard")
    failure_buckets = sorted(
        {
            str(bucket).strip()
            for bucket in _list_value(run.get("failure_buckets"))
            if str(bucket).strip()
        }
    )
    fact_ids.extend(f"{case_id}:failure_bucket:{bucket}" for bucket in failure_buckets)
    harness_warning_signal_count = _nested_int(run, "harness_result", "warning_signal_count")
    if harness_warning_signal_count is not None:
        fact_ids.append(f"{case_id}:harness_warning_signal_count")
    stop_gate_blocked = _nested_bool(run, "stop_gate_result", "blocked")
    stop_gate_reason_code = _nested_text(run, "stop_gate_result", "reason_code")
    if stop_gate_blocked is not None:
        fact_ids.append(f"{case_id}:stop_gate_blocked")
    if stop_gate_reason_code:
        fact_ids.append(f"{case_id}:stop_gate_reason:{stop_gate_reason_code}")
    return SkillFlowAnalysisCase(
        case_id=case_id,
        status=status,
        verdict=verdict,
        quality_optimization_score=score,
        failure_buckets=failure_buckets,
        harness_warning_signal_count=harness_warning_signal_count,
        stop_gate_blocked=stop_gate_blocked,
        stop_gate_reason_code=stop_gate_reason_code,
        fact_ids=fact_ids,
    )


def _quality_score(run: Mapping[str, Any]) -> float | None:
    raw_scorecard = run.get("quality_scorecard")
    if not isinstance(raw_scorecard, Mapping):
        return None
    return QualityScorecard.model_validate(raw_scorecard).optimization_score


def _nested_text(run: Mapping[str, Any], parent: str, child: str) -> str | None:
    payload = run.get(parent)
    if not isinstance(payload, Mapping):
        return None
    value = str(payload.get(child) or "").strip()
    return value or None


def _nested_int(run: Mapping[str, Any], parent: str, child: str) -> int | None:
    payload = run.get(parent)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(child)
    return value if isinstance(value, int) and value >= 0 else None


def _nested_bool(run: Mapping[str, Any], parent: str, child: str) -> bool | None:
    payload = run.get(parent)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(child)
    return value if isinstance(value, bool) else None


def _list_value(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _analysis_prompt(request: SkillFlowAnalysisRequest) -> str:
    payload = request.model_dump_json(indent=2)
    return (
        "你是离线 Skill Flow 诊断器。仅使用输入中的结构化事实；不要补充、猜测或复述"
        "问题、答案、原始工具内容。输出必须包含事实观察、共性问题、初步归因假设、"
        "参考流程草案、回归风险、建议动作和置信度。每条 observation 与 hypothesis 必须"
        "引用至少一个输入 fact_id。你的结论只是假设，不能授权 Skill 修改或发布。\n\n"
        f"输入事实包：\n{payload}"
    )
