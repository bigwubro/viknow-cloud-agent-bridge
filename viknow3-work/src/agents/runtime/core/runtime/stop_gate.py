"""Immediate stop-candidate gate for agent final answers."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agents.runtime.core.answer_review import AnswerReviewCitationPolicy, CitationPolicyMode
from agents.runtime.core.answer_review.domain import STOP_GATE_CHECK_TOOL_NAME
from agents.runtime.core.runtime.domain import ToolCallRecord


class StopGateReasonCode(StrEnum):
    PASSED = "passed"
    MISSING_CITATION = "missing_citation"
    INVALID_CITATION = "invalid_citation"
    ACTION_INTENT_WITHOUT_TOOL = "action_intent_without_tool"
    NOT_AN_ANSWER = "not_an_answer"
    UNINSPECTED_KNOWLEDGE_EVIDENCE = "uninspected_knowledge_evidence"
    ANSWER_PLAN_INCOMPLETE = "answer_plan_incomplete"
    JUDGE_UNAVAILABLE = "judge_unavailable"


class StopGateNextAction(StrEnum):
    PUBLISH = "publish"
    CONTINUE_TOOL_USE = "continue_tool_use"
    REWRITE_WITH_CITATIONS = "rewrite_with_citations"
    REFUSE_INSUFFICIENT_EVIDENCE = "refuse_insufficient_evidence"
    RESPOND_WITH_LIMITATIONS = "respond_with_limitations"


class StopGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    reason_code: StopGateReasonCode
    next_action: StopGateNextAction = StopGateNextAction.PUBLISH
    reason: str = ""
    continuation_instruction: str | None = None

    @classmethod
    def passed_decision(cls) -> StopGateDecision:
        return cls(
            passed=True,
            reason_code=StopGateReasonCode.PASSED,
            next_action=StopGateNextAction.PUBLISH,
            reason="stop candidate is publishable",
        )


class StopGateJudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publishable: bool
    reason_code: StopGateReasonCode
    next_action: StopGateNextAction
    reason: str = ""


class StopGateInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    question: str
    answer: str
    citation_policy: AnswerReviewCitationPolicy
    resolved_citation_count: int = Field(ge=0)
    citation_errors: list[str] = Field(default_factory=list)
    eligible_claims: list[dict[str, Any]] = Field(default_factory=list)
    completed_tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    answer_plan: dict[str, Any] | None = None


class StopGateJudge(Protocol):
    def judge_stop_candidate(self, request: StopGateInput) -> StopGateJudgeResult:
        """Return a lightweight publishability judgment for one stop candidate."""


class LLMStopGateJudge:
    """Lightweight LLM stop-candidate judge."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def judge_stop_candidate(self, request: StopGateInput) -> StopGateJudgeResult:
        from agents.runtime.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self.llm,
            StopGateJudgeResult,
            _judge_prompt(request),
            run_name="immediate_stop_gate_judge",
            inherit_callbacks=True,
            max_attempts=1,
            max_tokens=512,
        )
        if result is None:
            raise ValueError("immediate stop gate judge returned no result")
        return result


class StopCandidateGate:
    """Gate no-tool assistant stop candidates before final answer publication."""

    def __init__(
        self,
        *,
        judge: StopGateJudge | None = None,
        max_judge_calls_per_run: int = 1,
    ) -> None:
        self._judge = judge
        self._max_judge_calls_per_run = max(max_judge_calls_per_run, 0)
        self._judge_calls = 0

    def evaluate(self, request: StopGateInput) -> StopGateDecision:
        rule_decision = _rule_decision(request)
        if rule_decision is not None:
            return rule_decision
        if self._judge is None or self._judge_calls >= self._max_judge_calls_per_run:
            return StopGateDecision.passed_decision()
        self._judge_calls += 1
        try:
            result = self._judge.judge_stop_candidate(request)
        except Exception as exc:
            return StopGateDecision(
                passed=True,
                reason_code=StopGateReasonCode.JUDGE_UNAVAILABLE,
                reason=f"stop gate judge unavailable: {exc}",
            )
        if result.publishable:
            return StopGateDecision.passed_decision()
        return StopGateDecision(
            passed=False,
            reason_code=result.reason_code,
            next_action=result.next_action,
            reason=result.reason,
            continuation_instruction=_continuation_instruction(request, result),
        )


def _rule_decision(request: StopGateInput) -> StopGateDecision | None:
    answer = request.answer.strip()
    if _not_answer_shape(answer):
        return _blocked(
            request,
            StopGateReasonCode.NOT_AN_ANSWER,
            StopGateNextAction.CONTINUE_TOOL_USE,
            "stop candidate is not a final answer",
        )
    if _contains_action_intent(answer):
        return _blocked(
            request,
            StopGateReasonCode.ACTION_INTENT_WITHOUT_TOOL,
            StopGateNextAction.CONTINUE_TOOL_USE,
            "stop candidate promises more tool work instead of calling tools",
        )
    if request.citation_errors:
        return _blocked(
            request,
            StopGateReasonCode.INVALID_CITATION,
            StopGateNextAction.REWRITE_WITH_CITATIONS,
            "; ".join(request.citation_errors),
        )
    if request.eligible_claims and _contains_bare_internal_evidence_id(answer):
        return _blocked(
            request,
            StopGateReasonCode.INVALID_CITATION,
            StopGateNextAction.REWRITE_WITH_CITATIONS,
            "回答中包含裸内部证据标识，请仅使用可解析的 citation token",
        )
    if _has_uninspected_search_evidence(request) and not _is_no_evidence_refusal(answer):
        return _blocked(
            request,
            StopGateReasonCode.UNINSPECTED_KNOWLEDGE_EVIDENCE,
            StopGateNextAction.CONTINUE_TOOL_USE,
            "knowledge_search returned evidence but no eligible inspect claim was cited",
        )
    if _citation_required(request) and request.resolved_citation_count == 0:
        if _is_no_evidence_refusal(answer):
            return StopGateDecision.passed_decision()
        return _blocked(
            request,
            StopGateReasonCode.MISSING_CITATION,
            StopGateNextAction.REWRITE_WITH_CITATIONS,
            "citation policy requires citations but the answer has no resolved citation",
        )
    return None


def _judge_prompt(request: StopGateInput) -> str:
    # stop_gate_check 是 runtime 门控反馈工具，不属于证据工具链，不给 judge 展示。
    tool_names = [
        call.name for call in request.completed_tool_calls if call.name != STOP_GATE_CHECK_TOOL_NAME
    ]
    # Judge 需要看到候选引用的全部 claim（不截断）。之前截断到前 8 条时，
    # 候选引用 [10]/[11] 这类高编号会被 judge 误判为 invalid_citation。
    claim_payload = [
        {
            "tool": claim.get("tool"),
            "claim": claim.get("claim"),
            "agent_citation": claim.get("agent_citation"),
        }
        for claim in request.eligible_claims
    ]
    existing_tokens = {
        str(claim.get("agent_citation") or "").strip("[]")
        for claim in request.eligible_claims
        if claim.get("agent_citation")
    }
    cited_markers = [
        {"marker": f"[{token}]", "resolvable": token in existing_tokens}
        for token in _CITED_MARKER_RE.findall(request.answer)
    ]
    return (
        "你是 ViKnow Immediate Stop Gate 的轻量判定器。"
        "只判断这条 stop candidate 是否可以发布，不评估完整答案质量分数，"
        "不重新检索，不引入外部知识。输出必须符合结构化 schema。\n\n"
        "判定规则：\n"
        "- publishable=true: 这条消息已经直接回答或合理拒答，可以进入发布链路。\n"
        "- publishable=false: 这条消息仍是行动意图、过程说明、明显未回答、缺少必要引用，"
        "或应继续工具调用/改写引用/拒答证据不足。\n"
        "- 候选引用的编号已列在「候选引用的编号」且 resolvable=true 时，"
        "不得以 invalid_citation 拒绝；invalid_citation 仅用于编号无法解析的情形。\n"
        "- Answer Plan 是可调整的调查建议，不是额外的用户要求。"
        "以原问题的范围和已核验证据判断答案是否充分，不能仅因 full_asset、"
        "目录未遍历或未分类就拒绝已有充分局部证据的答案。\n"
        "- 原问题确实要求全局计数、完整列举或不存在性结论时，检查现有证据"
        "是否支持该范围；局部命中不能自动证明整体完整，目录全部读过也不等于"
        "所有相关内容已核验。范围不足可用 answer_plan_incomplete 说明具体缺口。\n"
        "- 区分用户要求与 Agent 的解释假设：修订计划不等于用户同意换题。"
        "发现资料范围不同或另一个指标可计算，不能代替原问题要求的范围或指标；"
        "有歧义时允许说明解释、提供有条件的答案或请求澄清。\n"
        "- 局部核验结果不能写成整体总数或不存在性结论，再靠末尾限制语免责。"
        "检查结论本身是否与已承认的缺口一致；合理的阶段性答复或澄清请求可以发布，"
        "不必为了消除所有不确定性强制继续检索。\n"
        "- 计划中的 pending/not_found 是 Agent 的进度记录，不直接决定答案能否发布。"
        "结合原问题、实际答案和证据判断；未更新的事项不自动代表答案错误。\n"
        "- agent_review 是 Agent 自查，不是权威判定。review_is_current=false 表示缺失或过期，"
        "不能当成已完成审核；无论是否自查，都独立核对原问题的遗漏、反证和范围限制。\n"
        "- 若当前 agent_review 的 uncertainties 非空，最终答案必须说明不明确之处，"
        "并覆盖其中所有有证据支持的 possible_answers；在证据没有消除歧义时，不得把其中"
        "一个可能答案声明为唯一答案，也不得用用户未提供、证据未支持的惯例或偏好代替依据。\n"
        "- next_action 只能选择 publish、continue_tool_use、rewrite_with_citations、"
        "refuse_insufficient_evidence、respond_with_limitations。\n"
        "- reason_code 选择最贴近的原因代码；reason 用一句话说明。\n\n"
        f"用户问题：{request.question}\n"
        f"stop candidate：{request.answer}\n"
        f"citation_policy：{request.citation_policy.model_dump(mode='json')}\n"
        f"已解析引用数：{request.resolved_citation_count}\n"
        f"引用错误：{request.citation_errors}\n"
        f"工具调用：{tool_names}\n"
        f"Answer Plan：{json.dumps(request.answer_plan, ensure_ascii=False)}\n"
        f"候选引用的编号：{json.dumps(cited_markers, ensure_ascii=False)}\n"
        f"eligible claims：{json.dumps(claim_payload, ensure_ascii=False)}\n"
    )


def _blocked(
    request: StopGateInput,
    reason_code: StopGateReasonCode,
    next_action: StopGateNextAction,
    reason: str,
) -> StopGateDecision:
    result = StopGateJudgeResult(
        publishable=False,
        reason_code=reason_code,
        next_action=next_action,
        reason=reason,
    )
    return StopGateDecision(
        passed=False,
        reason_code=reason_code,
        next_action=next_action,
        reason=reason,
        continuation_instruction=_continuation_instruction(request, result),
    )


def _citation_required(request: StopGateInput) -> bool:
    if request.eligible_claims:
        return True
    policy = request.citation_policy
    if policy.mode != CitationPolicyMode.REQUIRED or not policy.evidence_required:
        return False
    if policy.reason in {"answer_has_citations", "claim_tools_produced_claims"}:
        return True
    return policy.reason == "knowledge_evidence_available"


def _continuation_instruction(
    request: StopGateInput,
    result: StopGateJudgeResult,
) -> str:
    if result.reason_code == StopGateReasonCode.ANSWER_PLAN_INCOMPLETE:
        plan_context = request.answer_plan or {}
        uncertainties = (
            (plan_context.get("agent_review") or {}).get("uncertainties") or []
            if plan_context.get("review_is_current") is True
            else []
        )
        if uncertainties:
            return (
                "当前自查仍有以下未解决解释："
                + json.dumps(uncertainties, ensure_ascii=False)
                + "。最终答案必须明确说明问题口径不确定，并逐项给出有证据支持的可能答案；"
                "在证据没有消除歧义时，不得声明其中一个是唯一答案。"
            )
    action_hint = {
        StopGateNextAction.CONTINUE_TOOL_USE: (
            "请直接调用必要的工具完成查证；不要只说明将要查看、搜索或确认。"
        ),
        StopGateNextAction.REWRITE_WITH_CITATIONS: (
            "已有可引用的 inspect 工具结果。请回看对应 inspect 工具结果，"
            "遵循当前 skill 的 citation rules 重写答案；每个事实只使用实际支持它的 "
            "citation token，不要仅为满足格式追加标签。"
        ),
        StopGateNextAction.REFUSE_INSUFFICIENT_EVIDENCE: (
            "不要基于未验证结果作答；若无法继续，请明确说明现有资料不足。"
        ),
        StopGateNextAction.RESPOND_WITH_LIMITATIONS: (
            "请先说明当前核验状态，再依据已验证信息回答用户问题；"
            "无法确认的部分必须明确说明限制，不要承诺继续查证或补造事实。"
        ),
        StopGateNextAction.PUBLISH: "重新给出可以发布的最终答案。",
    }[result.next_action]
    if result.next_action != StopGateNextAction.REWRITE_WITH_CITATIONS:
        return action_hint
    # 可用 token 由 stop_gate_check 结果的 available_tokens 字段提供唯一来源，
    # instruction 不再内嵌列表，避免同一份 token 列表重复出现。
    return action_hint


_CITED_MARKER_RE = re.compile(r"\[(\d+)\]")
_BARE_INTERNAL_EVIDENCE_ID_RE = re.compile(
    r"\b(?:chunk-[0-9a-f]{16,}|"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}_seg_\d+)\b",
    re.IGNORECASE,
)


def _contains_bare_internal_evidence_id(answer: str) -> bool:
    return _BARE_INTERNAL_EVIDENCE_ID_RE.search(answer) is not None


_ACTION_INTENT_PATTERNS = (
    "我去看",
    "我来看",
    "我再看",
    "我查一下",
    "我去查",
    "我再查",
    "我确认一下",
    "我需要查看",
    "我需要检查",
    "我将查看",
    "我会查看",
    "let me check",
    "i will check",
    "i'll check",
    "i need to inspect",
    "i will search",
)


def _contains_action_intent(answer: str) -> bool:
    normalized = answer.strip().lower()
    return any(marker in normalized for marker in _ACTION_INTENT_PATTERNS)


def _not_answer_shape(answer: str) -> bool:
    normalized = answer.strip().lower()
    if not normalized:
        return True
    return normalized in {
        "处理中",
        "我明白了",
        "我知道了",
        "好的",
        "收到",
        "understood",
    }


def _is_no_evidence_refusal(answer: str) -> bool:
    normalized = answer.strip().lower()
    return any(marker in normalized for marker in _NO_EVIDENCE_MARKERS)


_NO_EVIDENCE_MARKERS = (
    "没有找到",
    "未找到",
    "没有相关",
    "资料不足",
    "信息不足",
    "无法根据现有资料",
    "无法根据当前资料",
    "无法确认",
    "no relevant",
    "not enough information",
    "insufficient information",
)


def _has_uninspected_search_evidence(request: StopGateInput) -> bool:
    if request.eligible_claims or request.resolved_citation_count > 0:
        return False
    return any(
        call.name == "knowledge_search" and _knowledge_search_evidence_count(call.result) > 0
        for call in request.completed_tool_calls
    )


def _knowledge_search_evidence_count(result: str | None) -> int:
    if not result:
        return 0
    try:
        import json

        payload = json.loads(result)
    except (TypeError, ValueError):
        return 0
    from agents.runtime.core.tools.base import unwrap_tool_data

    payload = unwrap_tool_data(payload) if isinstance(payload, dict) else {}
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        return 0
    return sum(1 for item in evidence if isinstance(item, dict))


__all__ = [
    "LLMStopGateJudge",
    "StopCandidateGate",
    "StopGateDecision",
    "StopGateInput",
    "StopGateJudge",
    "StopGateJudgeResult",
    "StopGateNextAction",
    "StopGateReasonCode",
]
