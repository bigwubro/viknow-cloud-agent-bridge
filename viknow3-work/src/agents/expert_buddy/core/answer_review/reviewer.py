"""Answer reviewer service ports."""

from __future__ import annotations

import re
from typing import Any, Protocol

from loguru import logger
from openai import APITimeoutError

from agents.expert_buddy.core.answer_review.domain import (
    AnswerReviewCitation,
    AnswerReviewClaim,
    AnswerReviewClaimRelevancePair,
    AnswerReviewClaimRelevanceScore,
    AnswerReviewClaimReview,
    AnswerReviewClaimReviewRequest,
    AnswerReviewDimensionScores,
    AnswerReviewDimensionStatus,
    AnswerReviewDimensionStatuses,
    AnswerReviewRequest,
    AnswerReviewResult,
    AnswerReviewRisk,
    AnswerReviewSemanticReview,
    AnswerReviewSemanticReviewRequest,
    AnswerReviewSpan,
    CitationPolicyMode,
    ClaimSupportStatus,
    RiskSeverity,
)


class AnswerReviewer(Protocol):
    """Port implemented by answer review services."""

    def review(self, request: AnswerReviewRequest) -> AnswerReviewResult:
        """Review a draft answer against the provided evidence bundle."""


class AnswerReviewClaimReviewer(Protocol):
    """Optional claim extraction and citation coverage reviewer."""

    def review_claims(self, request: AnswerReviewClaimReviewRequest) -> AnswerReviewClaimReview:
        """Review answer claims against cited evidence."""


class AnswerReviewClaimRelevanceScorer(Protocol):
    """Optional rerank-style relevance scorer for answer spans and cited claims."""

    def score_relevance(
        self, pairs: list[AnswerReviewClaimRelevancePair]
    ) -> list[AnswerReviewClaimRelevanceScore]:
        """Score answer-span and cited-claim relevance pairs."""


class AnswerReviewSemanticReviewer(Protocol):
    """Optional semantic consistency and intent coverage reviewer."""

    def review_semantics(
        self, request: AnswerReviewSemanticReviewRequest
    ) -> AnswerReviewSemanticReview:
        """Review answer consistency and intent coverage."""


def _log_review_internal_stage(
    stage: str,
    *,
    level: str = "info",
    **extra: Any,
) -> None:
    bound = logger.bind(answer_review_internal_stage=stage, **extra)
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


class RuleBasedAnswerReviewer:
    """Deterministic first-pass reviewer for local gating and tests."""

    def __init__(
        self,
        *,
        pass_threshold: int = 75,
        claim_reviewer: AnswerReviewClaimReviewer | None = None,
        semantic_reviewer: AnswerReviewSemanticReviewer | None = None,
        claim_relevance_scorer: AnswerReviewClaimRelevanceScorer | None = None,
        weak_relevance_threshold: float = 0.7,
        low_relevance_threshold: float = 0.45,
    ) -> None:
        self.pass_threshold = pass_threshold
        self.claim_reviewer = claim_reviewer
        self.semantic_reviewer = semantic_reviewer
        self.claim_relevance_scorer = claim_relevance_scorer
        self.weak_relevance_threshold = weak_relevance_threshold
        self.low_relevance_threshold = low_relevance_threshold

    def review(self, request: AnswerReviewRequest) -> AnswerReviewResult:
        _log_review_internal_stage(
            "review_started",
            attempt=request.attempt,
            answer_chars=len(request.answer or ""),
            citations=len(request.citations),
            citation_errors=len(request.citation_errors),
            citation_policy=request.citation_policy.mode,
        )
        citation_required = request.citation_policy.mode == CitationPolicyMode.REQUIRED
        citation_missing_allowed = request.citation_policy.mode in {
            CitationPolicyMode.OPTIONAL,
            CitationPolicyMode.NOT_EXPECTED,
            CitationPolicyMode.REFUSAL_NO_EVIDENCE,
        }
        # 简单问题豁免已暂停：不再调用 is_simple_unreferenced_question 判定，
        # simple_unreferenced_answer 恒为 False，机制保留待后续评估是否修改逻辑。
        simple_unreferenced_answer = False
        claim_support_applicable = not (
            (simple_unreferenced_answer or citation_missing_allowed)
            and not request.citations
            and not request.citation_errors
        )
        risks: list[AnswerReviewRisk] = []
        answer_spans = _answer_review_spans(request.answer, request.citations)

        if (
            citation_required
            and not request.citations
            and not request.citation_errors
            and not simple_unreferenced_answer
        ):
            risks.append(
                AnswerReviewRisk(
                    id="risk-missing-citation",
                    severity=RiskSeverity.HIGH,
                    kind="missing_citation",
                    message="答案没有标注可核验的证据引用。",
                )
            )
        if _needs_visual_ground_review(request):
            risks.append(
                AnswerReviewRisk(
                    id="risk-missing-visual-ground",
                    severity=RiskSeverity.HIGH,
                    kind="missing_visual_ground",
                    message=(
                        "计数问题已经使用知识库或视频证据，但没有调用 visual_ground "
                        "生成专用计数 claim。"
                    ),
                )
            )
        if _needs_video_count_inspect_review(request):
            risks.append(
                AnswerReviewRisk(
                    id="risk-missing-video-count-inspect",
                    severity=RiskSeverity.HIGH,
                    kind="missing_video_count_inspect",
                    message=(
                        "计数问题已经使用知识库或视觉定位工具，但没有调用 video_inspect、"
                        "text_inspect、image_inspect 或 document_inspect "
                        "生成可核验的片段事实 claim。"
                    ),
                )
            )
        for index, error in enumerate(request.citation_errors, start=1):
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-invalid-citation-{index}",
                    severity=RiskSeverity.HIGH,
                    kind="invalid_citation",
                    message=f"答案包含无法解析的引用：{error}。",
                )
            )
        risks.extend(_citation_quality_risks(request.citations))
        risks.extend(_counting_citation_contradiction_risks(request, answer_spans))
        risks.extend(
            _span_claim_relevance_risks(
                answer_spans,
                request.citations,
                scorer=self.claim_relevance_scorer,
                weak_threshold=self.weak_relevance_threshold,
                low_threshold=self.low_relevance_threshold,
            )
        )
        _log_review_internal_stage(
            "rule_scores_ready",
            spans=len(answer_spans),
            risks=len(risks),
            claim_support_applicable=claim_support_applicable,
            simple_unreferenced_answer=simple_unreferenced_answer,
        )

        citation_validity = (
            100
            if (request.citations and not request.citation_errors)
            or (
                (simple_unreferenced_answer or citation_missing_allowed)
                and not request.citation_errors
            )
            else 40
        )
        claim_support = _claim_support_rule_score(
            answer_spans,
            simple_unreferenced_answer=simple_unreferenced_answer,
            citation_missing_allowed=citation_missing_allowed,
            applicable=claim_support_applicable,
        )
        if simple_unreferenced_answer or (
            citation_missing_allowed and not request.citation_errors and not request.citations
        ):
            citation_validity = 100
        elif request.citation_errors:
            citation_validity = min(citation_validity, 30)
        citation_validity = _risk_citation_validity_score_cap(risks, citation_validity)
        from agents.expert_buddy.core.multimodal.structured_llm import collect_structured_llm_traces

        with collect_structured_llm_traces() as structured_llm_calls:
            claim_review, claim_review_fallback_reason = (
                _run_claim_reviewer(self.claim_reviewer, request, answer_spans)
                if claim_support_applicable
                else (None, "claim_support_not_applicable")
            )
            if claim_review is not None:
                claim_review = _normalize_claim_review(
                    claim_review,
                    request.citations,
                    answer_spans,
                )
                risks.extend(claim_review.risks)
                claim_support = _claim_support_review_score(
                    claim_review, default_score=claim_support
                )
                claim_support = _claim_support_score_cap(claim_review, claim_support)
            claim_support = _risk_claim_support_score_cap(risks, claim_support)
            reviewed_claims = (
                claim_review.claims
                if claim_review is not None and claim_review.claims
                else _rule_based_claims(
                    request.answer,
                    answer_spans,
                    request.citations,
                    claim_support_applicable=claim_support_applicable,
                    simple_unreferenced_answer=simple_unreferenced_answer,
                    citation_missing_allowed=citation_missing_allowed,
                    citation_policy_reason=request.citation_policy.reason,
                )
            )
            semantic_review, semantic_review_fallback_reason = _run_semantic_reviewer(
                self.semantic_reviewer,
                request,
                answer_spans,
                reviewed_claims,
            )
        semantic_evaluable = semantic_review is not None
        semantic_failed = semantic_review_fallback_reason == "semantic_reviewer_failed"
        contradiction = _semantic_score(
            semantic_review.contradiction_score if semantic_review is not None else None,
            default=0,
        )
        coverage = _semantic_score(
            semantic_review.coverage_score if semantic_review is not None else None,
            default=0,
        )
        if semantic_review is not None:
            risks.extend(semantic_review.risks)
            risks = _merge_risks(risks)
        dimension_statuses = AnswerReviewDimensionStatuses(
            claim_support=AnswerReviewDimensionStatus.SCORED
            if claim_support_applicable
            else AnswerReviewDimensionStatus.NOT_APPLICABLE,
            contradiction=AnswerReviewDimensionStatus.SCORED
            if semantic_evaluable
            else AnswerReviewDimensionStatus.NOT_EVALUABLE,
            coverage=AnswerReviewDimensionStatus.SCORED
            if semantic_evaluable
            else AnswerReviewDimensionStatus.NOT_EVALUABLE,
        )
        scores = AnswerReviewDimensionScores(
            citation_validity=citation_validity,
            claim_support=claim_support,
            contradiction=contradiction,
            coverage=coverage,
        )
        overall = _overall_score(scores, dimension_statuses)
        passed = (
            not semantic_failed
            and overall >= self.pass_threshold
            and not any(risk.severity == RiskSeverity.HIGH for risk in risks)
        )
        _log_review_internal_stage(
            "review_completed",
            overall_score=overall,
            passed=passed,
            risks=len(risks),
            claims=len(reviewed_claims),
            claim_support_source="llm" if claim_review is not None else "rule_fallback",
            semantic_review_source="llm" if semantic_review is not None else "rule_fallback",
        )

        requested_ids = [slot.get("id") for slot in request.answer_slots]
        assessed_ids = (
            [slot.slot_id for slot in semantic_review.slot_assessments] if semantic_review else []
        )
        if (
            any(not isinstance(value, str) or not value.strip() for value in requested_ids)
            or (len(requested_ids) != len(set(requested_ids)))
            or (request.choice_options and not requested_ids)
        ):
            execution_status = "invalid_input"
        elif semantic_review is None or claim_review_fallback_reason in {
            "claim_reviewer_failed",
            "claim_reviewer_timeout",
        }:
            execution_status = "unavailable"
        elif set(requested_ids) != set(assessed_ids) or len(assessed_ids) != len(set(assessed_ids)):
            execution_status = "invalid_output"
        else:
            execution_status = "completed"

        return AnswerReviewResult(
            execution_status=execution_status,
            overall_score=overall,
            passed=passed,
            retryable=not passed and not semantic_failed,
            dimension_scores=scores,
            dimension_statuses=dimension_statuses,
            summary=(
                "语义审查失败，无法确认答案的内容自洽和意图覆盖。"
                if semantic_failed
                else "语义审查未完成，无法确认答案的内容自洽和意图覆盖。"
                if not semantic_evaluable
                else "答案通过可靠性检查。"
                if passed
                else "答案可靠性不足，需要修正引用或证据支撑。"
            ),
            retry_instruction=_retry_instruction(risks),
            citation_policy=request.citation_policy,
            claims=reviewed_claims,
            risks=risks,
            citations=_citation_payloads(request.citations),
            selected_choice=semantic_review.selected_choice if semantic_review else None,
            slot_assessments=semantic_review.slot_assessments if semantic_review else [],
            review_trace=_answer_review_trace(
                claim_review=claim_review,
                fallback_reason=claim_review_fallback_reason,
                semantic_review=semantic_review,
                semantic_fallback_reason=semantic_review_fallback_reason,
                structured_llm_calls=structured_llm_calls,
            ),
        )


def _valid_citation_refs(citations: list[AnswerReviewCitation]) -> list[str]:
    refs: list[str] = []
    for citation in citations:
        ref = citation.claim_ref or citation.claim_id or citation.marker
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _citation_quality_risks(citations: list[AnswerReviewCitation]) -> list[AnswerReviewRisk]:
    risks: list[AnswerReviewRisk] = []
    for citation in citations:
        evidence_id = citation.claim_ref or citation.claim_id or citation.marker
        if citation.confidence is not None and citation.confidence < 0.6:
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-low-confidence-{_risk_ref(citation)}",
                    severity=RiskSeverity.MEDIUM,
                    kind="low_confidence_claim",
                    message=f"引用 {citation.marker} 对应 claim 的置信度低于阈值。",
                    evidence_ids=[evidence_id] if evidence_id else [],
                )
            )
        if citation.tool == "text_inspect" and citation.quote_verified is False:
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-unverified-quote-{_risk_ref(citation)}",
                    severity=RiskSeverity.MEDIUM,
                    kind="unverified_quote",
                    message=f"引用 {citation.marker} 对应文本 claim 的原文 quote 未通过验证。",
                    evidence_ids=[evidence_id] if evidence_id else [],
                )
            )
    return risks


def _counting_citation_contradiction_risks(
    request: AnswerReviewRequest,
    spans: list[AnswerReviewSpan],
) -> list[AnswerReviewRisk]:
    """Reject count statements backed by a grounding claim with a different count."""
    if not _COUNTING_QUESTION_RE.search(request.question or ""):
        return []
    citations_by_marker = {citation.marker: citation for citation in request.citations}
    risks: list[AnswerReviewRisk] = []
    for span in spans:
        answer_counts = _explicit_count_values(span.text)
        if not any(value > 0 for value in answer_counts):
            continue
        for marker in span.citation_markers:
            citation = citations_by_marker.get(marker)
            if citation is None or citation.tool != "visual_ground":
                continue
            claim_counts = _explicit_count_values(citation.claim)
            if not claim_counts or answer_counts & claim_counts:
                continue
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-counting-citation-contradiction-{_risk_id_token(marker)}",
                    severity=RiskSeverity.HIGH,
                    kind="counting_citation_contradiction",
                    message=(
                        f"计数结论中的数量 {sorted(answer_counts)} 由 {marker} 引用，但该 "
                        f"visual_ground claim 的检测数量为 {sorted(claim_counts)}。"
                    ),
                    evidence_ids=[citation.claim_ref or citation.claim_id or marker],
                )
            )
    return risks


def _explicit_count_values(text: str) -> set[int]:
    units = "个种次项位层台支张根颗瓣块条片"
    values = {int(match) for match in re.findall(rf"(?<![.\d])(\d+)\s*[{units}]", text or "")}
    chinese_values = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for match in re.findall(rf"([零一二两三四五六七八九十])\s*[{units}]", text or ""):
        values.add(chinese_values[match])
    if "未检测到目标" in (text or ""):
        values.add(0)
    return values


def _citation_payloads(citations: list[AnswerReviewCitation]) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for citation in citations:
        marker = _citation_marker_key(citation)
        if not marker:
            continue
        payloads[marker] = {
            "marker": citation.marker,
            "claim_id": citation.claim_id,
            "citation_index": citation.citation_index,
            "tool": citation.tool,
            "claim": citation.claim,
            "confidence": citation.confidence,
            "quote_verified": citation.quote_verified,
            "verify_reason": citation.verify_reason,
            "source": citation.source,
            "render": citation.render,
            "claim_ref": citation.claim_ref,
        }
    return payloads


def _run_claim_reviewer(
    claim_reviewer: AnswerReviewClaimReviewer | None,
    request: AnswerReviewRequest,
    answer_spans: list[AnswerReviewSpan],
) -> tuple[AnswerReviewClaimReview | None, str]:
    if claim_reviewer is None:
        _log_review_internal_stage("claim_reviewer_skipped", reason="claim_reviewer_not_configured")
        return None, "claim_reviewer_not_configured"
    if request.citation_policy.mode != CitationPolicyMode.REQUIRED:
        _log_review_internal_stage("claim_reviewer_skipped", reason="citation_policy_not_required")
        return None, "citation_policy_not_required"
    if not request.citations or request.citation_errors:
        _log_review_internal_stage(
            "claim_reviewer_skipped",
            reason="missing_or_invalid_citations",
            citations=len(request.citations),
            citation_errors=len(request.citation_errors),
        )
        return None, "missing_or_invalid_citations"
    try:
        _log_review_internal_stage(
            "claim_reviewer_start",
            spans=len(answer_spans),
            citations=len(request.citations),
        )
        claim_review = claim_reviewer.review_claims(
            AnswerReviewClaimReviewRequest(
                question=request.question,
                answer=request.answer,
                spans=answer_spans,
                citations=request.citations,
                citation_policy=request.citation_policy,
            )
        )
        if not claim_review.claims:
            _log_review_internal_stage(
                "claim_reviewer_completed",
                claims=0,
                risks=len(claim_review.risks),
                reason="claim_reviewer_returned_no_claims",
            )
            return None, "claim_reviewer_returned_no_claims"
        _log_review_internal_stage(
            "claim_reviewer_completed",
            claims=len(claim_review.claims),
            risks=len(claim_review.risks),
            claim_support_score=claim_review.claim_support_score,
        )
        return claim_review, ""
    except Exception as exc:
        fallback_reason = (
            "claim_reviewer_timeout"
            if isinstance(exc, (APITimeoutError, TimeoutError))
            else "claim_reviewer_failed"
        )
        _log_review_internal_stage(
            fallback_reason,
            error=str(exc),
            level="warning",
        )
        return None, fallback_reason


def _run_semantic_reviewer(
    semantic_reviewer: AnswerReviewSemanticReviewer | None,
    request: AnswerReviewRequest,
    answer_spans: list[AnswerReviewSpan],
    claims: list[AnswerReviewClaim],
) -> tuple[AnswerReviewSemanticReview | None, str]:
    if semantic_reviewer is None:
        _log_review_internal_stage(
            "semantic_reviewer_skipped",
            reason="semantic_reviewer_not_configured",
        )
        return None, "semantic_reviewer_not_configured"
    if not request.answer.strip():
        _log_review_internal_stage("semantic_reviewer_skipped", reason="empty_answer")
        return None, "empty_answer"
    try:
        _log_review_internal_stage(
            "semantic_reviewer_start",
            spans=len(answer_spans),
            claims=len(claims),
        )
        semantic_review = semantic_reviewer.review_semantics(
            AnswerReviewSemanticReviewRequest(
                question=request.question,
                answer=request.answer,
                spans=answer_spans,
                claims=claims,
                choice_options=request.choice_options,
                answer_slots=request.answer_slots,
                evidence_facts=request.evidence_facts,
                citation_policy=request.citation_policy,
            )
        )
        _log_review_internal_stage(
            "semantic_reviewer_completed",
            contradiction_score=semantic_review.contradiction_score,
            coverage_score=semantic_review.coverage_score,
            risks=len(semantic_review.risks),
            conflicts=len(semantic_review.conflicts),
            intent_units=len(semantic_review.intent_units),
        )
        return semantic_review, ""
    except Exception as exc:
        _log_review_internal_stage(
            "semantic_reviewer_failed",
            error=str(exc),
            level="warning",
        )
        return None, "semantic_reviewer_failed"


def _answer_review_trace(
    *,
    claim_review: AnswerReviewClaimReview | None,
    fallback_reason: str,
    semantic_review: AnswerReviewSemanticReview | None,
    semantic_fallback_reason: str,
    structured_llm_calls: list[dict],
) -> dict[str, Any]:
    if claim_review is None:
        trace = {
            "claim_support_source": "rule_fallback",
            "claim_support_fallback_reason": fallback_reason,
        }
    else:
        trace = {"claim_support_source": "llm"}
    if semantic_review is None:
        trace["semantic_review_source"] = "rule_fallback"
        trace["semantic_review_fallback_reason"] = semantic_fallback_reason
    else:
        trace["semantic_review_source"] = "llm"
        trace["semantic_review"] = {
            "contradiction_score": semantic_review.contradiction_score,
            "coverage_score": semantic_review.coverage_score,
            "conflicts": list(semantic_review.conflicts),
            "intent_units": list(semantic_review.intent_units),
            "notes": semantic_review.notes,
        }
    if structured_llm_calls:
        trace["structured_llm_calls"] = structured_llm_calls
    return trace


def _semantic_score(value: int | None, *, default: int) -> int:
    if value is None:
        return default
    return max(0, min(100, value))


def _span_support_risks(
    spans: list[AnswerReviewSpan],
    *,
    citation_required: bool,
) -> list[AnswerReviewRisk]:
    if not citation_required or not any(span.citation_markers for span in spans):
        return []
    risks: list[AnswerReviewRisk] = []
    for span in spans:
        if span.citation_markers or not span.text.strip():
            continue
        risks.append(
            AnswerReviewRisk(
                id=f"risk-uncited-answer-{span.id}",
                severity=RiskSeverity.MEDIUM,
                kind="uncited_answer_span",
                message=f"答案片段没有直接关联引用：{span.text}",
            )
        )
    return risks


def _span_claim_relevance_risks(
    spans: list[AnswerReviewSpan],
    citations: list[AnswerReviewCitation],
    *,
    scorer: AnswerReviewClaimRelevanceScorer | None,
    weak_threshold: float,
    low_threshold: float,
) -> list[AnswerReviewRisk]:
    if scorer is None:
        return []
    pairs = _span_claim_relevance_pairs(spans, citations)
    if not pairs:
        return []
    try:
        scores = scorer.score_relevance(pairs)
    except Exception:
        return []
    pairs_by_id = {pair.id: pair for pair in pairs}
    risks: list[AnswerReviewRisk] = []
    for score in scores:
        pair = pairs_by_id.get(score.pair_id)
        if pair is None:
            continue
        if score.score < low_threshold:
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-low-claim-relevance-{_risk_id_token(pair.id)}",
                    severity=RiskSeverity.MEDIUM,
                    kind="low_claim_relevance",
                    message=(
                        f"答案片段与引用 claim 的 rerank 相关性较低"
                        f"（score={score.score:.3f}）：{pair.answer_span}"
                    ),
                    evidence_ids=[pair.cited_claim_ref] if pair.cited_claim_ref else [],
                )
            )
        elif score.score < weak_threshold:
            risks.append(
                AnswerReviewRisk(
                    id=f"risk-weak-claim-relevance-{_risk_id_token(pair.id)}",
                    severity=RiskSeverity.MEDIUM,
                    kind="weak_claim_relevance",
                    message=(
                        f"答案片段与引用 claim 的 rerank 相关性偏弱"
                        f"（score={score.score:.3f}）：{pair.answer_span}"
                    ),
                    evidence_ids=[pair.cited_claim_ref] if pair.cited_claim_ref else [],
                )
            )
    return risks


def _span_claim_relevance_pairs(
    spans: list[AnswerReviewSpan],
    citations: list[AnswerReviewCitation],
) -> list[AnswerReviewClaimRelevancePair]:
    citations_by_marker = {citation.marker: citation for citation in citations if citation.marker}
    pairs: list[AnswerReviewClaimRelevancePair] = []
    for span in spans:
        if not span.text.strip():
            continue
        for marker in span.citation_markers:
            citation = citations_by_marker.get(marker)
            if citation is None or not citation.claim.strip():
                continue
            pairs.append(
                AnswerReviewClaimRelevancePair(
                    id=f"{span.id}:{marker}",
                    span_id=span.id,
                    citation_marker=marker,
                    answer_span=span.text,
                    cited_claim=citation.claim,
                    cited_claim_ref=citation.claim_ref or citation.claim_id or marker,
                )
            )
    return pairs


def _claim_support_rule_score(
    spans: list[AnswerReviewSpan],
    *,
    applicable: bool,
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
) -> int:
    if not applicable:
        return 0
    if simple_unreferenced_answer:
        return 85
    if not spans:
        return 0
    if any(span.citation_markers for span in spans):
        return 90
    if citation_missing_allowed:
        return 60
    return 50


def _risk_claim_support_score_cap(
    risks: list[AnswerReviewRisk],
    claim_support: int,
) -> int:
    if any(risk.kind == "unsupported_claim" for risk in risks):
        return min(claim_support, 35)
    if any(
        risk.kind
        in {
            "low_confidence_claim",
            "unverified_quote",
            "weak_claim_support",
            "low_claim_relevance",
        }
        for risk in risks
    ):
        claim_support = min(claim_support, 60)
    if any(risk.kind == "weak_claim_relevance" for risk in risks):
        claim_support = min(claim_support, 70)
    return claim_support


def _rule_based_claims(
    answer: str,
    spans: list[AnswerReviewSpan],
    citations: list[AnswerReviewCitation],
    *,
    claim_support_applicable: bool,
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
    citation_policy_reason: str,
) -> list[AnswerReviewClaim]:
    if not claim_support_applicable:
        return []
    if not spans:
        text = answer.strip()
        if not text:
            return []
        spans = [
            AnswerReviewSpan(
                id="span-1",
                text=text,
                citation_markers=[],
                cited_claim_refs=[],
            )
        ]
    has_cited_span = any(span.citation_markers for span in spans)
    return [
        AnswerReviewClaim(
            id=f"claim-{index}",
            text=span.text,
            support_status=_span_claim_support_status(
                span,
                has_cited_span=has_cited_span,
                simple_unreferenced_answer=simple_unreferenced_answer,
                citation_missing_allowed=citation_missing_allowed,
            ),
            evidence_ids=span.cited_claim_refs,
            evidence_details=_evidence_details_for_ids(span.cited_claim_refs, citations=citations),
            span_ids=[span.id],
            notes=_span_claim_notes(
                span,
                has_cited_span=has_cited_span,
                citation_policy_reason=citation_policy_reason,
                simple_unreferenced_answer=simple_unreferenced_answer,
                citation_missing_allowed=citation_missing_allowed,
            ),
        )
        for index, span in enumerate(spans, start=1)
        if span.text.strip()
    ]


def _span_claim_support_status(
    span: AnswerReviewSpan,
    *,
    has_cited_span: bool,
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
) -> ClaimSupportStatus:
    if span.citation_markers or simple_unreferenced_answer:
        return ClaimSupportStatus.SUPPORTED
    if citation_missing_allowed or has_cited_span:
        return ClaimSupportStatus.WEAK
    return ClaimSupportStatus.UNSUPPORTED


def _span_claim_notes(
    span: AnswerReviewSpan,
    *,
    has_cited_span: bool,
    citation_policy_reason: str,
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
) -> str:
    if simple_unreferenced_answer:
        return "简单问题无需检索证据，基于问题类型放宽引用要求。"
    if span.citation_markers:
        return "基于该答案片段附近的显式引用进行第一版规则审查。"
    if citation_missing_allowed:
        return f"引用策略允许不强制引用：{citation_policy_reason}；该状态不代表事实已由证据支撑。"
    if has_cited_span:
        return "该答案片段没有直接关联引用，暂按弱支撑记录。"
    return "答案没有直接关联引用，无法在 claim_support 中判断证据支撑。"


def _answer_review_spans(
    answer: str,
    citations: list[AnswerReviewCitation],
) -> list[AnswerReviewSpan]:
    marker_refs = {
        citation.marker: citation.claim_ref or citation.claim_id or citation.marker
        for citation in citations
        if citation.marker
    }
    if not marker_refs:
        text = answer.strip()
        return (
            [
                AnswerReviewSpan(
                    id="span-1",
                    text=text,
                    citation_markers=[],
                    cited_claim_refs=[],
                )
            ]
            if text
            else []
        )

    marker_pattern = re.compile(
        "|".join(re.escape(marker) for marker in sorted(marker_refs, key=len, reverse=True))
    )
    spans: list[AnswerReviewSpan] = []
    pending_text_start = 0
    active_span_start = 0
    active_text = ""
    active_markers: list[str] = []
    active_refs: list[str] = []

    for match in marker_pattern.finditer(answer):
        marker = match.group(0)
        if active_text and _only_gap_between(answer, pending_text_start, match.start()):
            _append_unique(active_markers, marker)
            _append_unique(active_refs, marker_refs[marker])
            pending_text_start = match.end()
            if answer[match.end() : match.end() + 1] == "[":
                continue
            forward_text, forward_end = _forward_scope_text(answer, match.end())
            if forward_text and _opens_forward_citation_scope(active_text):
                active_text = f"{active_text}\n{forward_text}"
                pending_text_start = forward_end
            spans.append(
                _answer_review_span(
                    len(spans) + 1,
                    active_text,
                    active_markers,
                    active_refs,
                )
            )
            active_span_start = match.end()
            active_text = ""
            active_markers = []
            active_refs = []
            continue

        if pending_text_start < match.start():
            active_span_start = _left_span_boundary(
                answer,
                match.start(),
                min_start=pending_text_start,
            )
            uncited_text = _clean_span_text(answer[pending_text_start:active_span_start])
            if uncited_text:
                spans.append(_answer_review_span(len(spans) + 1, uncited_text, [], []))
            active_text = _clean_span_text(answer[active_span_start : match.start()])
            _append_unique(active_markers, marker)
            _append_unique(active_refs, marker_refs[marker])
        elif not active_text:
            active_span_start = _left_span_boundary(
                answer,
                match.start(),
                min_start=pending_text_start,
            )
            active_text = _clean_span_text(answer[active_span_start : match.start()])
            _append_unique(active_markers, marker)
            _append_unique(active_refs, marker_refs[marker])

        pending_text_start = match.end()

        next_char = answer[match.end() : match.end() + 1]
        if next_char == "[":
            continue
        if active_text:
            forward_text, forward_end = _forward_scope_text(answer, match.end())
            if forward_text and _opens_forward_citation_scope(active_text):
                active_text = f"{active_text}\n{forward_text}"
                pending_text_start = forward_end
            spans.append(
                _answer_review_span(
                    len(spans) + 1,
                    active_text,
                    active_markers,
                    active_refs,
                )
            )
        active_span_start = match.end()
        active_text = ""
        active_markers = []
        active_refs = []

    trailing_text = _clean_span_text(answer[pending_text_start:])
    if trailing_text:
        spans.append(_answer_review_span(len(spans) + 1, trailing_text, [], []))
    return spans


def _answer_review_span(
    index: int,
    text: str,
    citation_markers: list[str],
    cited_claim_refs: list[str],
) -> AnswerReviewSpan:
    return AnswerReviewSpan(
        id=f"span-{index}",
        text=text,
        citation_markers=list(citation_markers),
        cited_claim_refs=list(cited_claim_refs),
    )


def _left_span_boundary(answer: str, marker_start: int, *, min_start: int) -> int:
    index = marker_start - 1
    while index >= 0 and answer[index].isspace():
        index -= 1
    if index >= min_start and answer[index] in _STRONG_SPAN_BOUNDARIES:
        index -= 1
    while index >= 0:
        if index < min_start:
            return min_start
        if answer[index] in _STRONG_SPAN_BOUNDARIES:
            return index + 1
        if answer[index] in _WEAK_SPAN_BOUNDARIES:
            return index + 1
        index -= 1
    return 0


_STRONG_SPAN_BOUNDARIES = "\n。！？!?；;"
_WEAK_SPAN_BOUNDARIES = ""


def _clean_span_text(text: str) -> str:
    return text.strip().lstrip(_LEADING_SPAN_SEPARATORS).strip()


def _only_gap_between(answer: str, start: int, end: int) -> bool:
    return not answer[start:end].strip()


def _forward_scope_text(answer: str, marker_end: int) -> tuple[str, int]:
    index = marker_end
    while index < len(answer) and answer[index] in " \t":
        index += 1
    if index >= len(answer) or answer[index] != "\n":
        return "", marker_end
    while index < len(answer) and answer[index].isspace():
        index += 1
    if index >= len(answer):
        return "", marker_end
    text = _clean_span_text(answer[index:])
    return text, len(answer)


def _opens_forward_citation_scope(text: str) -> bool:
    normalized = text.strip()
    if not normalized.endswith(("：", ":")):
        return False
    return any(
        token in normalized
        for token in (
            "包括",
            "如下",
            "具体操作",
            "步骤",
            "分别为",
            "清单",
        )
    )


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


_LEADING_SPAN_SEPARATORS = "。！？!?；;，,"


def _normalize_claim_review(
    claim_review: AnswerReviewClaimReview,
    citations: list[AnswerReviewCitation],
    spans: list[AnswerReviewSpan],
) -> AnswerReviewClaimReview:
    default_evidence_ids = _valid_citation_refs(citations)
    claims = _merge_claims(
        [
            claim.model_copy(
                update={
                    "evidence_ids": _dedupe_strings(claim.evidence_ids or default_evidence_ids),
                    "evidence_details": _evidence_details_for_ids(
                        claim.evidence_ids or default_evidence_ids,
                        citations=citations,
                    ),
                    "span_ids": _dedupe_strings(
                        claim.span_ids or _infer_claim_span_ids(claim, spans)
                    ),
                }
            )
            for claim in claim_review.claims
        ]
    )
    risks = _merge_risks(list(claim_review.risks))
    existing_risk_keys = {
        (risk.kind, tuple(risk.evidence_ids), risk.message) for risk in claim_review.risks
    }
    for index, claim in enumerate(claims, start=1):
        if claim.support_status == ClaimSupportStatus.UNSUPPORTED:
            risk = AnswerReviewRisk(
                id=f"risk-unsupported-claim-{index}",
                severity=RiskSeverity.HIGH,
                kind="unsupported_claim",
                message=f"答案 claim 未被引用证据支持：{claim.text}",
                evidence_ids=claim.evidence_ids,
            )
        elif claim.support_status == ClaimSupportStatus.WEAK:
            risk = AnswerReviewRisk(
                id=f"risk-weak-claim-support-{index}",
                severity=RiskSeverity.MEDIUM,
                kind="weak_claim_support",
                message=f"答案 claim 只有弱证据支撑：{claim.text}",
                evidence_ids=claim.evidence_ids,
            )
        else:
            continue
        risk_key = (risk.kind, tuple(risk.evidence_ids), risk.message)
        if risk_key not in existing_risk_keys:
            risks.append(risk)
            existing_risk_keys.add(risk_key)
    return claim_review.model_copy(update={"claims": claims, "risks": _merge_risks(risks)})


def _evidence_details_for_ids(
    evidence_ids: list[str],
    *,
    citations: list[AnswerReviewCitation],
) -> list[dict[str, Any]]:
    if not evidence_ids or not citations:
        return []
    citations_by_ref: dict[str, AnswerReviewCitation] = {}
    for citation in citations:
        for ref in (
            citation.claim_ref,
            citation.claim_id,
            citation.marker,
            _citation_marker_key(citation),
        ):
            if ref and ref not in citations_by_ref:
                citations_by_ref[ref] = citation
    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_id in evidence_ids:
        citation = citations_by_ref.get(evidence_id)
        if citation is None:
            continue
        ref = citation.claim_ref or citation.claim_id or citation.marker
        if ref in seen:
            continue
        seen.add(ref)
        detail = {
            "marker": citation.marker,
            "claim_ref": citation.claim_ref,
            "claim_id": citation.claim_id,
            "tool": citation.tool,
            "claim": citation.claim,
            "confidence": citation.confidence,
        }
        details.append({key: value for key, value in detail.items() if value not in (None, "")})
    return details


def _infer_claim_span_ids(
    claim: AnswerReviewClaim,
    spans: list[AnswerReviewSpan],
) -> list[str]:
    claim_text = _normalized_claim_text(claim.text)
    if not claim_text:
        return []
    matched: list[str] = []
    for span in spans:
        span_text = _normalized_claim_text(span.text)
        if not span_text:
            continue
        if claim_text in span_text or span_text in claim_text:
            matched.append(span.id)
    return matched


def _merge_claims(claims: list[AnswerReviewClaim]) -> list[AnswerReviewClaim]:
    merged: list[AnswerReviewClaim] = []
    for claim in claims:
        target_index = _matching_claim_index(merged, claim)
        if target_index is None:
            merged.append(claim)
            continue
        existing = merged[target_index]
        merged[target_index] = existing.model_copy(
            update={
                "text": _merged_claim_text(existing.text, claim.text),
                "support_status": _worse_support_status(
                    existing.support_status,
                    claim.support_status,
                ),
                "evidence_ids": _dedupe_strings(existing.evidence_ids + claim.evidence_ids),
                "evidence_details": _merge_evidence_details(
                    existing.evidence_details,
                    claim.evidence_details,
                ),
                "span_ids": _dedupe_strings(existing.span_ids + claim.span_ids),
                "notes": _join_notes(existing.notes, claim.notes),
            }
        )
    return merged


def _merge_risks(risks: list[AnswerReviewRisk]) -> list[AnswerReviewRisk]:
    merged: list[AnswerReviewRisk] = []
    index_by_key: dict[tuple[RiskSeverity, str, str], int] = {}
    for risk in risks:
        key = (risk.severity, risk.kind, risk.message)
        target_index = index_by_key.get(key)
        if target_index is None:
            index_by_key[key] = len(merged)
            merged.append(
                risk.model_copy(update={"evidence_ids": _dedupe_strings(risk.evidence_ids)})
            )
            continue
        existing = merged[target_index]
        merged[target_index] = existing.model_copy(
            update={
                "evidence_ids": _dedupe_strings(existing.evidence_ids + risk.evidence_ids),
            }
        )
    return merged


def _matching_claim_index(
    claims: list[AnswerReviewClaim],
    claim: AnswerReviewClaim,
) -> int | None:
    normalized = _normalized_claim_text(claim.text)
    if not normalized:
        return None
    for index, existing in enumerate(claims):
        existing_normalized = _normalized_claim_text(existing.text)
        if not existing_normalized:
            continue
        if normalized == existing_normalized:
            return index
        if normalized in existing_normalized or existing_normalized in normalized:
            return index
    return None


def _normalized_claim_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    normalized = re.sub(r"[，,。.!！?？；;：:、（）()【】\\[\\]`*_\\-]+", "", normalized)
    for token in ("所以", "因此", "也就是说", "根据知识库中", "文档"):
        normalized = normalized.replace(token, "")
    return normalized


def _merged_claim_text(left: str, right: str) -> str:
    left_normalized = _normalized_claim_text(left)
    right_normalized = _normalized_claim_text(right)
    if left_normalized and right_normalized and left_normalized in right_normalized:
        return right
    return left


def _worse_support_status(
    left: ClaimSupportStatus,
    right: ClaimSupportStatus,
) -> ClaimSupportStatus:
    severity = {
        ClaimSupportStatus.SUPPORTED: 0,
        ClaimSupportStatus.WEAK: 1,
        ClaimSupportStatus.UNSUPPORTED: 2,
    }
    return left if severity[left] >= severity[right] else right


def _join_notes(left: str, right: str) -> str:
    notes = _dedupe_strings([left, right])
    return "；".join(note for note in notes if note)


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _merge_evidence_details(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for detail in [*left, *right]:
        key = str(
            detail.get("claim_ref")
            or detail.get("claim_id")
            or detail.get("marker")
            or detail.get("claim")
            or ""
        )
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(detail)
    return merged


def _claim_support_score_cap(
    claim_review: AnswerReviewClaimReview,
    claim_support: int,
) -> int:
    if any(claim.support_status == ClaimSupportStatus.UNSUPPORTED for claim in claim_review.claims):
        return min(claim_support, 35)
    if any(claim.support_status == ClaimSupportStatus.WEAK for claim in claim_review.claims):
        return min(claim_support, 60)
    return claim_support


def _claim_support_review_score(
    claim_review: AnswerReviewClaimReview,
    *,
    default_score: int,
) -> int:
    if not claim_review.claims:
        return default_score
    supported_count = sum(
        claim.support_status == ClaimSupportStatus.SUPPORTED for claim in claim_review.claims
    )
    weak_count = sum(
        claim.support_status == ClaimSupportStatus.WEAK for claim in claim_review.claims
    )
    unsupported_count = sum(
        claim.support_status == ClaimSupportStatus.UNSUPPORTED for claim in claim_review.claims
    )
    total = supported_count + weak_count + unsupported_count
    if total == 0:
        return default_score
    raw_score = round(
        ((supported_count * 100) + (weak_count * 60) + (unsupported_count * 35)) / total
    )
    return min(default_score, raw_score)


def _risk_citation_validity_score_cap(
    risks: list[AnswerReviewRisk],
    citation_validity: int,
) -> int:
    if any(risk.kind == "invalid_citation" for risk in risks):
        return min(citation_validity, 30)
    if any(risk.kind == "missing_citation" for risk in risks):
        return min(citation_validity, 40)
    return citation_validity


def _overall_score(
    scores: AnswerReviewDimensionScores,
    statuses: AnswerReviewDimensionStatuses,
) -> int:
    values = [
        value
        for key, value in scores.model_dump().items()
        if getattr(statuses, key) == AnswerReviewDimensionStatus.SCORED
    ]
    if not values:
        return 0
    return round(sum(values) / len(values))


def _claim_support_status(
    *,
    valid_citations: list[str],
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
) -> ClaimSupportStatus:
    if valid_citations or simple_unreferenced_answer:
        return ClaimSupportStatus.SUPPORTED
    if citation_missing_allowed:
        return ClaimSupportStatus.WEAK
    return ClaimSupportStatus.UNSUPPORTED


def _claim_notes(
    *,
    citation_policy_reason: str,
    has_valid_citations: bool,
    simple_unreferenced_answer: bool,
    citation_missing_allowed: bool,
) -> str:
    if simple_unreferenced_answer:
        return "简单问题无需检索证据，基于问题类型放宽引用要求。"
    if citation_missing_allowed and not has_valid_citations:
        return f"引用策略允许不强制引用：{citation_policy_reason}；该状态不代表事实已由证据支撑。"
    return "基于答案中的显式证据引用进行第一版规则审查。"


def _citation_marker_key(citation: AnswerReviewCitation) -> str:
    if citation.citation_index is not None:
        return str(citation.citation_index)
    marker = citation.marker.strip()
    if marker.startswith("[") and marker.endswith("]"):
        return marker[1:-1]
    return marker


def _risk_ref(citation: AnswerReviewCitation) -> str:
    return citation.claim_id or str(citation.citation_index or "").strip() or "citation"


def _risk_id_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "pair"


def is_simple_unreferenced_question(question: str) -> bool:
    """Return whether a question can be answered without evidence citations."""

    normalized = question.strip().lower()
    if not normalized:
        return False
    simple_markers = (
        "你是谁",
        "你是什么",
        "你叫什么",
        "介绍一下你",
        "介绍你自己",
        "介绍自己",
        "你能做什么",
        "你可以做什么",
        "你有什么能力",
        "你好",
        "您好",
        "谢谢",
        "hi",
        "hello",
        "thanks",
        "help",
    )
    return any(marker in normalized for marker in simple_markers)


_COUNTING_QUESTION_RE = re.compile(
    r"(几片|几个|几条|几种|几次|几项|几位|几层|几台|几支|几张|几根|几颗|几瓣|几块|"
    r"多少(?:个|种|次|项|位|层|台|支|张|根|颗|瓣|块)?|数量|计数|"
    r"how\s+many|\bcount\b|number\s+of)",
    re.IGNORECASE,
)


def _needs_visual_ground_review(request: AnswerReviewRequest) -> bool:
    """Static visible-object counts must cite a visual_ground claim."""

    plan_modalities = _answer_plan_required_modalities(request.answer_plan)
    if plan_modalities and not (plan_modalities & {"video", "image"}):
        return False
    if not _is_static_visible_count_question(request.question or ""):
        return False
    tool_names = set(request.citation_policy.tool_names)
    if any(citation.tool == "visual_ground" for citation in request.citations):
        return False
    evidence_tools = {
        "knowledge_search",
        "list_video_segments",
        "video_inspect",
        "video_inspect_2_0",
    }
    return bool(tool_names & evidence_tools)


def _answer_plan_required_modalities(answer_plan: dict[str, object] | None) -> set[str]:
    if not isinstance(answer_plan, dict):
        return set()
    slots = answer_plan.get("slots")
    if not isinstance(slots, list):
        return set()
    return {
        str(modality)
        for slot in slots
        if isinstance(slot, dict)
        for modality in slot.get("required_modalities") or []
    }


_NON_STATIC_COUNT_RE = re.compile(
    r"(几种|多少种|种类|几次|多少次|出现.{0,8}(?:几次|多少次)|"
    r"几位|小数|多久|时长|持续|周期|多少(?:秒|分钟|小时)|"
    r"字幕|文字|台词|参数|功能|工具|圈|转动|擦拭|流程中|"
    r"使用了|用了|耗用了|共用|要检查|拔了几个|拆了几个)",
    re.IGNORECASE,
)


def _is_static_visible_count_question(question: str) -> bool:
    if not _COUNTING_QUESTION_RE.search(question):
        return False
    return not _NON_STATIC_COUNT_RE.search(question)


def _needs_video_count_inspect_review(request: AnswerReviewRequest) -> bool:
    """Video/KB counting answers need an inspect claim beyond raw grounding."""

    if not _COUNTING_QUESTION_RE.search(request.question or ""):
        return False
    tool_names = set(request.citation_policy.tool_names)
    if tool_names & {"video_inspect", "text_inspect", "image_inspect", "document_inspect"}:
        return False
    if "uploaded_image_search" in tool_names and not (
        tool_names & {"knowledge_search", "list_video_segments"}
    ):
        return False
    return bool(tool_names & {"knowledge_search", "list_video_segments", "visual_ground"})


def _retry_instruction(risks: list[AnswerReviewRisk]) -> str:
    if not risks:
        return ""
    if any(risk.kind == "missing_visual_ground" for risk in risks):
        return (
            "请重新生成答案：这是静态可见物体计数问题，必须基于已召回或已检查的视频 evidence "
            "调用 `visual_ground`。如果没有精确帧时间，就把同一 evidence 的 "
            "library_id、asset_id、evidence_id 和完整计数问题传给 `visual_ground`，"
            "由工具使用片段中点做单帧计数；最终数量必须采用并引用 visual_ground "
            "返回的 claim。不要用冲突的 video_inspect 数量替代 grounding 结果。"
        )
    if any(risk.kind == "missing_video_count_inspect" for risk in risks):
        return (
            "请重新生成答案：这是知识库视频计数问题，必须先对选中的视频 evidence "
            "调用 `video_inspect` 验证片段、目标和最终状态。若问题问全视频累计次数、"
            "字幕/OCR出现次数或跨片段类别数，先用 `list_video_segments` 建立候选列表，"
            "逐一 inspect 所有候选并去重汇总，不得从搜索摘要或 grep 结果直接作答。"
            "只有静态可见物体计数才在同一 evidence 上继续调用 `visual_ground`。"
            "最终答案采用并引用匹配 count mode 的 inspect claim。"
        )
    if any(risk.kind == "counting_citation_contradiction" for risk in risks):
        return (
            "请重新生成答案：计数结论必须与引用的 visual_ground claim 检测数量一致。"
            "请重新选择正确视频片段和关键帧并调用 visual_ground；如果工具检测数量与答案"
            "不一致，不得把该 claim 作为计数依据。最终只复制与计数结论一致的 claim 引用。"
        )
    return (
        "请重新生成答案：每个关键结论都必须使用本轮 evidence 中存在的证据 ID 标注引用；"
        "删除没有证据支撑的结论；不要引入本轮证据外的新事实。"
    )
