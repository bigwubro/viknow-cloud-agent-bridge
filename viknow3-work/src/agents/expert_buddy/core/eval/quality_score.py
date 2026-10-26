"""Versioned, traceable quality scores for offline Skill Flow evaluation."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

RequirementStatus = Literal[
    "satisfied",
    "partial",
    "omitted",
    "contradicted",
    "not_evaluable",
]

_STATUS_SCORES: dict[RequirementStatus, float | None] = {
    "satisfied": 1.0,
    "partial": 0.5,
    "omitted": 0.0,
    "contradicted": 0.0,
    "not_evaluable": None,
}


class OracleRequirementAssessment(BaseModel):
    """One judge conclusion for an accepted Oracle requirement."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    status: RequirementStatus
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    citation_supported: bool | None = None
    event_sequences: list[int] = Field(default_factory=list)


class OracleRequirementJudgeRequest(BaseModel):
    """The limited offline context sent to a requirement judge."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    question: str
    answer: str
    requirements: list[dict[str, Any]] = Field(default_factory=list)


class OracleRequirementJudge(Protocol):
    """Offline semantic comparison port; it never changes Oracle facts."""

    model: str

    def evaluate(
        self, request: OracleRequirementJudgeRequest
    ) -> list[OracleRequirementAssessment]: ...


class _OracleRequirementJudgeResponse(BaseModel):
    assessments: list[OracleRequirementAssessment]


class ConfiguredOracleRequirementJudge:
    """Structured LLM judge configured exclusively for offline Skill Flow evals."""

    def __init__(self, *, chat_model: Any, model: str, max_tokens: int) -> None:
        self._chat_model = chat_model
        self.model = model
        self._max_tokens = max_tokens

    def evaluate(self, request: OracleRequirementJudgeRequest) -> list[OracleRequirementAssessment]:
        from agents.expert_buddy.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self._chat_model,
            _OracleRequirementJudgeResponse,
            _judge_prompt(request),
            run_name="skill_flow_oracle_requirement_judge",
            max_attempts=1,
            max_tokens=self._max_tokens,
        )
        if result is None:
            raise ValueError("oracle requirement judge returned no valid structured result")
        expected_ids = [str(item["requirement_id"]) for item in request.requirements]
        actual_ids = [item.requirement_id for item in result.assessments]
        if sorted(actual_ids) != sorted(expected_ids) or len(actual_ids) != len(set(actual_ids)):
            raise ValueError("oracle requirement judge must assess every requirement exactly once")
        return result.assessments


def build_oracle_requirement_judge(judge_model: Any) -> ConfiguredOracleRequirementJudge:
    """Build the configured offline judge without affecting runtime tool configs."""

    from agents.expert_buddy.core.config import ModelProvider

    if judge_model.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported Skill Flow oracle judge provider: {judge_model.provider}")
    base_url = judge_model.resolve_base_url()
    if not base_url:
        raise RuntimeError("Skill Flow oracle judge base_url or base_url_env is required")
    from langchain_openai import ChatOpenAI

    return ConfiguredOracleRequirementJudge(
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


class QualityRuleResult(BaseModel):
    """One deterministic quality invariant and its normalized result."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_code: str = ""
    event_sequences: list[int] = Field(default_factory=list)


class QualityDimensionScore(BaseModel):
    """One independently inspectable quality dimension."""

    model_config = ConfigDict(extra="forbid")

    score: float | None = Field(default=None, ge=0.0, le=1.0)
    rules: list[QualityRuleResult] = Field(default_factory=list)


class QualityScorecard(BaseModel):
    """The versioned scorecard consumed by reports and optimization clients."""

    model_config = ConfigDict(extra="forbid")

    scorer_version: str
    judge_model: str | None = None
    answer_evidence: QualityDimensionScore
    process_compliance: QualityDimensionScore
    efficiency_convergence: QualityDimensionScore
    release_safety: QualityDimensionScore
    optimization_score: float | None = Field(default=None, ge=0.0, le=100.0)
    not_evaluable_requirement_count: int = Field(default=0, ge=0)


def score_quality_scorecard(
    *,
    requirements: list[OracleRequirementAssessment],
    process_rules: list[QualityRuleResult],
    efficiency_rules: list[QualityRuleResult],
    release_rules: list[QualityRuleResult],
    scorer_version: str,
    judge_model: str | None,
) -> QualityScorecard:
    """Build the approved four-dimension score without hiding raw rule outcomes."""

    answer_rules = [_requirement_rule(assessment) for assessment in requirements]
    answer_evidence = QualityDimensionScore(
        score=_average(rule.score for rule in answer_rules),
        rules=answer_rules,
    )
    process_compliance = QualityDimensionScore(
        score=_average(rule.score for rule in process_rules),
        rules=process_rules,
    )
    efficiency_convergence = QualityDimensionScore(
        score=_average(rule.score for rule in efficiency_rules),
        rules=efficiency_rules,
    )
    release_safety = QualityDimensionScore(
        score=_average(rule.score for rule in release_rules),
        rules=release_rules,
    )
    weighted_dimensions = (
        (0.65, answer_evidence.score),
        (0.15, process_compliance.score),
        (0.10, efficiency_convergence.score),
        (0.10, release_safety.score),
    )
    applicable_weight = sum(weight for weight, score in weighted_dimensions if score is not None)
    optimization_score = (
        round(
            100
            * sum(
                weight * float(score) for weight, score in weighted_dimensions if score is not None
            )
            / applicable_weight,
            4,
        )
        if applicable_weight
        else None
    )
    return QualityScorecard(
        scorer_version=scorer_version,
        judge_model=judge_model,
        answer_evidence=answer_evidence,
        process_compliance=process_compliance,
        efficiency_convergence=efficiency_convergence,
        release_safety=release_safety,
        optimization_score=optimization_score,
        not_evaluable_requirement_count=sum(
            assessment.status == "not_evaluable" for assessment in requirements
        ),
    )


def score_process_compliance(
    *,
    signals: list[dict[str, Any]],
    tool_call_records: list[dict[str, Any]],
) -> QualityDimensionScore:
    """Score only deterministic prerequisite violations, never a prescribed path."""

    rules: list[QualityRuleResult] = []
    for signal in signals:
        if signal.get("kind") != "tool_gate_blocked":
            continue
        attributes = signal.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        tool_name = str(attributes.get("tool_name") or "")
        recovered = any(
            record.get("name") == tool_name and record.get("status") == "completed"
            for record in tool_call_records
        )
        rules.append(
            QualityRuleResult(
                rule_id=f"tool_gate:{tool_name or 'unknown'}",
                score=0.5 if recovered else 0.0,
                reason_code=str(attributes.get("reason_code") or "tool_gate_blocked"),
                event_sequences=_event_sequences(signal),
            )
        )
    if rules:
        return QualityDimensionScore(score=_average(rule.score for rule in rules), rules=rules)
    if signals:
        return QualityDimensionScore(
            score=1.0,
            rules=[QualityRuleResult(rule_id="tool_gate_clean", score=1.0)],
        )
    return QualityDimensionScore()


def score_efficiency_convergence(
    *,
    signals: list[dict[str, Any]],
    tool_call_records: list[dict[str, Any]],
) -> QualityDimensionScore:
    """Score only deterministic loss of convergence, never ordinary tool count."""

    rules: list[QualityRuleResult] = []
    for signal in signals:
        kind = str(signal.get("kind") or "")
        if kind == "run_budget_exhausted":
            rules.append(
                QualityRuleResult(
                    rule_id="run_budget_exhausted",
                    score=0.0,
                    reason_code="run_budget_exhausted",
                    event_sequences=_event_sequences(signal),
                )
            )
            continue
        if kind != "tool_gate_blocked":
            continue
        attributes = signal.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        reason_code = str(attributes.get("reason_code") or "")
        if reason_code not in {
            "duplicate_successful_tool_call",
            "duplicate_successful_workspace_tool_call",
            "repeat_failed_tool_call",
        }:
            continue
        tool_name = str(attributes.get("tool_name") or "")
        recovered = any(
            record.get("name") == tool_name and record.get("status") == "completed"
            for record in tool_call_records
        )
        rules.append(
            QualityRuleResult(
                rule_id=f"repeated_call:{tool_name or 'unknown'}",
                score=0.5 if recovered else 0.0,
                reason_code=f"{reason_code}_recovered" if recovered else reason_code,
                event_sequences=_event_sequences(signal),
            )
        )
    if rules:
        return QualityDimensionScore(score=_average(rule.score for rule in rules), rules=rules)
    if signals:
        return QualityDimensionScore(
            score=1.0,
            rules=[QualityRuleResult(rule_id="convergence_clean", score=1.0)],
        )
    return QualityDimensionScore()


def score_release_safety(
    *,
    status: str,
    stop_gate: dict[str, Any],
    citation_results: dict[str, Any],
) -> QualityDimensionScore:
    """Score whether the final answer release boundary, not semantic correctness, held."""

    if not stop_gate.get("available"):
        return QualityDimensionScore()
    if stop_gate.get("released"):
        score = 0.0
        reason_code = "stop_gate_released"
    elif status == "completed" and not citation_results.get("citation_format_error"):
        score = 1.0
        reason_code = "stop_gate_recovered" if stop_gate.get("recovered") else "release_allowed"
    else:
        score = 0.5
        reason_code = "constrained_end"
    rule = QualityRuleResult(rule_id="stop_gate_release", score=score, reason_code=reason_code)
    return QualityDimensionScore(score=score, rules=[rule])


def _requirement_rule(assessment: OracleRequirementAssessment) -> QualityRuleResult:
    status_score = _STATUS_SCORES[assessment.status]
    score = (
        None
        if status_score is None
        else status_score * (1.0 if assessment.citation_supported else 0.0)
    )
    return QualityRuleResult(
        rule_id=f"requirement:{assessment.requirement_id}",
        score=score,
        reason_code=assessment.status,
        event_sequences=assessment.event_sequences,
    )


def _average(values: object) -> float | None:
    scores = [float(value) for value in values if isinstance(value, int | float)]
    return round(sum(scores) / len(scores), 4) if scores else None


def _event_sequences(signal: dict[str, Any]) -> list[int]:
    values = signal.get("event_sequences")
    return [value for value in values if isinstance(value, int)] if isinstance(values, list) else []


def _judge_prompt(request: OracleRequirementJudgeRequest) -> str:
    payload = {
        "case_id": request.case_id,
        "question": request.question,
        "answer": request.answer,
        "requirements": request.requirements,
    }
    return "\n".join(
        [
            "你是 ViKnow 离线 Skill Flow 评测器。只将最终答案与已人工采纳的 requirement 比对。",
            "不得使用外部知识；每项 requirement 的 accepted_facts 中任一替代事实满足即可。",
            "逐项输出且只输出 structured schema：",
            "- satisfied：答案完整满足 requirement；partial：只满足一部分；",
            "- omitted：答案未覆盖；contradicted：答案明确作出相反断言；",
            "- not_evaluable：给定 requirement 文本不足以判断。",
            "额外回答声明不在本次评分范围内。每个 requirement_id 必须且只能输出一次。",
            json.dumps(payload, ensure_ascii=False),
        ]
    )
