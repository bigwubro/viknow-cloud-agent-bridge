"""Citation policy detection for answer review."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol

from agents.runtimes.deepagents.core.answer_review.domain import (
    STOP_GATE_CHECK_TOOL_NAME,
    AnswerReviewCitationPolicy,
    AnswerReviewPolicyClassification,
    AnswerReviewPolicyClassificationRequest,
    CitationPolicyMode,
)


class CitationPolicyClassifier(Protocol):
    """Optional classifier for citation-policy gray areas."""

    def classify_policy(
        self, request: AnswerReviewPolicyClassificationRequest
    ) -> AnswerReviewPolicyClassification:
        """Return a citation policy suggestion for one gray-area answer."""


def determine_citation_policy(
    *,
    question: str,
    answer: str,
    completed_tool_calls: Iterable[Any],
    claim_bundle: list[dict[str, Any]],
    resolved_citation_count: int,
    policy_classifier: CitationPolicyClassifier | None = None,
    min_classifier_confidence: float = 0.7,
) -> AnswerReviewCitationPolicy:
    """Determine whether one draft answer should be citation-gated."""

    tool_calls = [call for call in completed_tool_calls if hasattr(call, "name")]
    tool_names = _unique_tool_names(tool_calls)
    evidence_tool_names = _evidence_tool_names(tool_names)
    search_evidence_count = _knowledge_search_evidence_count(tool_calls)
    has_search = "knowledge_search" in tool_names
    has_discover = "knowledge_discover" in tool_names

    if resolved_citation_count > 0:
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.REQUIRED,
            reason="answer_has_citations",
            evidence_required=True,
            tool_names=tool_names,
        )
    if claim_bundle:
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.REQUIRED,
            reason="claim_tools_produced_claims",
            evidence_required=True,
            tool_names=tool_names,
        )
    if has_search and search_evidence_count > 0:
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.REQUIRED,
            reason="knowledge_evidence_available",
            evidence_required=True,
            tool_names=tool_names,
        )
    if has_search and search_evidence_count == 0:
        if _answer_is_no_evidence_refusal(answer):
            return AnswerReviewCitationPolicy(
                mode=CitationPolicyMode.REFUSAL_NO_EVIDENCE,
                reason="empty_search_refusal",
                evidence_required=False,
                tool_names=tool_names,
            )
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.REQUIRED,
            reason="empty_search_answer_not_refusal",
            evidence_required=True,
            tool_names=tool_names,
        )
    if has_discover and not evidence_tool_names and _is_metadata_summary_question(question):
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.OPTIONAL,
            reason="knowledge_metadata_summary",
            evidence_required=False,
            tool_names=tool_names,
        )
    if not tool_names and _is_direct_input_question(question):
        return AnswerReviewCitationPolicy(
            mode=CitationPolicyMode.NOT_EXPECTED,
            reason="direct_input_answer",
            evidence_required=False,
            tool_names=[],
        )
    rule_policy = AnswerReviewCitationPolicy(
        mode=CitationPolicyMode.REQUIRED,
        reason="default_conservative",
        evidence_required=True,
        tool_names=tool_names,
    )
    if policy_classifier is None:
        return rule_policy
    try:
        classification = policy_classifier.classify_policy(
            AnswerReviewPolicyClassificationRequest(
                question=question,
                answer=answer,
                tool_names=tool_names,
                search_evidence_count=search_evidence_count,
                claim_count=len(claim_bundle),
                citation_count=resolved_citation_count,
                rule_policy=rule_policy,
            )
        )
    except Exception:
        return rule_policy
    if classification.confidence < min_classifier_confidence:
        return rule_policy
    return AnswerReviewCitationPolicy(
        mode=classification.mode,
        reason=classification.reason,
        evidence_required=classification.evidence_required,
        tool_names=tool_names,
    )


def _unique_tool_names(tool_calls: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for call in tool_calls:
        name = str(getattr(call, "name", "") or "")
        # stop_gate_check 是 runtime 门控反馈工具，不属于证据/核验工具链。
        if name and name != STOP_GATE_CHECK_TOOL_NAME and name not in names:
            names.append(name)
    return names


_EVIDENCE_TOOL_NAMES = frozenset(
    {
        "knowledge_search",
        "video_inspect",
        "video_inspect_2_0",
        "document_inspect",
        "image_inspect",
        "text_inspect",
        "evidence_asr",
        "evidence_ocr",
    }
)


def _evidence_tool_names(tool_names: Iterable[str]) -> list[str]:
    return [name for name in tool_names if name in _EVIDENCE_TOOL_NAMES]


def _knowledge_search_evidence_count(tool_calls: Iterable[Any]) -> int:
    count = 0
    for call in tool_calls:
        if getattr(call, "name", "") != "knowledge_search":
            continue
        payload = _parse_json_object(getattr(call, "result", None))
        if payload is None:
            continue
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            count += sum(1 for item in evidence if isinstance(item, dict))
    return count


def _answer_is_no_evidence_refusal(answer: str) -> bool:
    normalized = answer.strip().lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in _NO_EVIDENCE_ANSWER_MARKERS):
        return False
    return any(marker in normalized for marker in _NO_EVIDENCE_REFUSAL_MARKERS)


_NO_EVIDENCE_REFUSAL_MARKERS = (
    "没有找到",
    "未找到",
    "没有相关",
    "无相关",
    "暂无相关",
    "资料不足",
    "信息不足",
    "现有资料不足",
    "当前知识库中没有",
    "无法根据现有资料",
    "无法根据当前资料",
    "无法根据当前知识库",
    "无法确认",
    "not find",
    "no relevant",
    "not enough information",
    "insufficient information",
)

_NO_EVIDENCE_ANSWER_MARKERS = (
    "但",
    "但是",
    "不过",
    "然而",
    "仍然",
    "显示",
    "说明",
    "表明",
    "已经",
    "完成",
    "but",
    "however",
    "shows",
    "indicates",
)


def _is_metadata_summary_question(question: str) -> bool:
    normalized = question.strip().lower()
    if not normalized:
        return False
    markers = (
        "知识库里有什么",
        "知识库有什么",
        "有哪些知识库",
        "列出知识库",
        "有什么资料",
        "有哪些资料",
        "有哪些文档",
        "列出文档",
        "what is in the knowledge base",
        "list knowledge",
        "list documents",
    )
    return any(marker in normalized for marker in markers)


def _is_direct_input_question(question: str) -> bool:
    normalized = question.strip().lower()
    if not normalized:
        return False
    markers = (
        "这张图",
        "这幅图",
        "这个图片",
        "上传的图片",
        "这段文字",
        "我发的内容",
        "我上传的内容",
        "this image",
        "uploaded image",
        "this text",
    )
    return any(marker in normalized for marker in markers)


def _parse_json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
