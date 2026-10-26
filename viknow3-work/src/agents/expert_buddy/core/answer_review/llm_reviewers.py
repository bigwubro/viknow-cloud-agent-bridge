"""LLM-backed answer review classifiers."""

from __future__ import annotations

import json

from agents.expert_buddy.core.answer_review.domain import (
    AnswerReviewClaimReview,
    AnswerReviewClaimReviewRequest,
    AnswerReviewPolicyClassification,
    AnswerReviewPolicyClassificationRequest,
    AnswerReviewSemanticReview,
    AnswerReviewSemanticReviewRequest,
    ClaimSupportStatus,
    RiskSeverity,
)


class LLMPolicyClassifier:
    """Classify citation policy gray areas using the configured chat model."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def classify_policy(
        self, request: AnswerReviewPolicyClassificationRequest
    ) -> AnswerReviewPolicyClassification:
        from agents.expert_buddy.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self.llm,
            AnswerReviewPolicyClassification,
            _policy_prompt(request),
            run_name="answer_review_policy_classifier",
            max_attempts=1,
            max_tokens=512,
        )
        if result is None:
            raise ValueError("answer review policy classifier returned no result")
        return result


class LLMClaimReviewer:
    """Extract answer claims and judge citation coverage using the configured chat model."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def review_claims(self, request: AnswerReviewClaimReviewRequest) -> AnswerReviewClaimReview:
        from agents.expert_buddy.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self.llm,
            AnswerReviewClaimReview,
            _claim_review_prompt(request),
            run_name="answer_review_claim_reviewer",
            max_attempts=1,
            max_tokens=2048,
        )
        if result is None:
            raise ValueError("answer review claim reviewer returned no result")
        return result


class LLMSemanticReviewer:
    """Judge answer-internal consistency and user-intent coverage."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def review_semantics(
        self, request: AnswerReviewSemanticReviewRequest
    ) -> AnswerReviewSemanticReview:
        from agents.expert_buddy.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self.llm,
            AnswerReviewSemanticReview,
            _semantic_review_prompt(request),
            run_name="answer_review_semantic_reviewer",
            max_attempts=1,
            max_tokens=2048,
        )
        if result is None:
            raise ValueError("answer review semantic reviewer returned no result")
        return result


def _policy_prompt(request: AnswerReviewPolicyClassificationRequest) -> str:
    return (
        "你是 ViKnow 答案引用策略分类器。只判断这次答案是否需要引用，不判断事实真假。\n"
        "输出必须符合结构化 schema。低置信时 confidence 低于 0.7。\n\n"
        "分类规则：\n"
        "- required: 答案使用知识库事实、工具 claim、历史证据，或无法确认是否无需引用。\n"
        "- optional: 只描述知识库/文档元信息，不陈述具体知识库事实。\n"
        "- not_expected: 简单寒暄、自我介绍，或只基于用户直接提供内容回答且未使用知识库事实。\n"
        "- refusal_no_evidence: 已查询但无证据，答案只说明资料不足/无法回答，没有追加事实结论。\n\n"
        f"问题：{request.question}\n"
        f"答案：{request.answer}\n"
        f"工具名：{request.tool_names}\n"
        f"knowledge_search evidence 数：{request.search_evidence_count}\n"
        f"claim 数：{request.claim_count}\n"
        f"已解析引用数：{request.citation_count}\n"
        f"规则基线：{request.rule_policy.model_dump(mode='json')}\n"
    )


def _claim_review_prompt(request: AnswerReviewClaimReviewRequest) -> str:
    span_payload = [
        {
            "id": span.id,
            "text": span.text,
            "citation_markers": span.citation_markers,
            "cited_claim_refs": span.cited_claim_refs,
        }
        for span in request.spans
    ]
    citation_payload = [
        {
            "marker": citation.marker,
            "claim_id": citation.claim_id,
            "claim_ref": citation.claim_ref,
            "tool": citation.tool,
            "claim": citation.claim,
            "confidence": citation.confidence,
            "quote_verified": citation.quote_verified,
            "verify_reason": citation.verify_reason,
            "source": _bounded_citation_source(citation.source),
        }
        for citation in request.citations
    ]
    return (
        "你是 ViKnow 答案事实与引用覆盖审查器。请把答案拆成需要核验的事实 claim，"
        "并判断每个 claim 是否被答案引用的证据 claim 支持。\n"
        "只基于给定 citations 判断，不引入外部知识。输出必须符合结构化 schema。\n\n"
        "answer spans 是 runtime 按 citation marker 从最终答案中切出的候选文本片段。"
        "优先在每个 span 内拆分事实 claim，并使用该 span 的 citation_markers / "
        "cited_claim_refs 找到候选证据；无 citation 的 span 如果包含事实断言，"
        "应判为 unsupported。\n\n"
        "support_status 使用：\n"
        "- supported: 引用明确支持该事实。\n"
        "- weak: 引用部分相关但不完整、不确定或需要推断。\n"
        "- unsupported: 引用不支持、矛盾，或答案事实没有对应引用。\n\n"
        "输出约束：\n"
        "- 每个 claim 必须尽量填写 span_ids，值来自 answer spans 的 id。\n"
        "- 语义重复 claim 必须合并，不要把带引用表述和后续总结句拆成两个独立 claim。\n"
        "- unsupported 必须说明缺少的支撑点，例如缺少直接引用、引用只支持部分事实、"
        "或 citation claim 没有覆盖该断言。\n\n"
        "risk 规则：unsupported 的关键事实应给 high unsupported_claim；"
        "weak 的关键事实可给 medium weak_claim_support。\n\n"
        f"问题：{request.question}\n"
        f"答案：{request.answer}\n"
        f"引用策略：{request.citation_policy.model_dump(mode='json')}\n"
        f"answer spans：{json.dumps(span_payload, ensure_ascii=False)}\n"
        f"可用 citations：{json.dumps(citation_payload, ensure_ascii=False)}\n\n"
        "输出 claims 时字段要求：\n"
        f"- support_status 必须是 {[item.value for item in ClaimSupportStatus]}\n"
        "- span_ids 使用对应 answer span 的 id。\n"
        "- evidence_ids 使用对应 citation 的 claim_ref、claim_id 或 marker。\n"
        "输出 risks 时字段要求：\n"
        f"- severity 必须是 {[item.value for item in RiskSeverity]}\n"
        "- kind 优先使用 unsupported_claim 或 weak_claim_support。\n"
    )


_CITATION_SOURCE_LOCATOR_FIELDS = (
    "type",
    "library_id",
    "asset_id",
    "document_id",
    "evidence_id",
    "chunk_id",
    "segment_id",
    "source_image_id",
    "page",
    "page_number",
    "timestamp_start",
    "timestamp_end",
    "time_range",
)


def _bounded_citation_source(source: dict[str, object]) -> dict[str, object]:
    """Keep only stable locators needed to distinguish evidence claims."""
    bounded = {
        key: source[key]
        for key in _CITATION_SOURCE_LOCATOR_FIELDS
        if key in source and source[key] not in (None, "", [], {})
    }
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        nested = {
            key: metadata[key]
            for key in _CITATION_SOURCE_LOCATOR_FIELDS
            if key in metadata and metadata[key] not in (None, "", [], {})
        }
        if nested:
            bounded["metadata"] = nested
    return bounded


def _semantic_review_prompt(request: AnswerReviewSemanticReviewRequest) -> str:
    span_payload = [
        {
            "id": span.id,
            "text": span.text,
            "citation_markers": span.citation_markers,
            "cited_claim_refs": span.cited_claim_refs,
        }
        for span in request.spans
    ]
    claim_payload = [
        {
            "id": claim.id,
            "text": claim.text,
            "support_status": claim.support_status,
            "evidence_ids": claim.evidence_ids,
            "span_ids": claim.span_ids,
            "notes": claim.notes,
        }
        for claim in request.claims
    ]
    return (
        "你是 ViKnow 答案语义审查器。请同时评估两个维度：内容自洽和意图覆盖。\n"
        "根据给定问题、最终答案、answer spans、answer claims 和已核验证据判断；"
        "不要引入外部知识。复用已有核验结果，定位条件缺口及事实到选项的映射。\n"
        "输出必须符合结构化 schema。\n\n"
        "选择题：保持原始 option_id 和 option_text，不得改写选项。先根据 supported "
        "claim 确认事实，再映射选项；selected_choice 填原始 ID、原文及支撑证据引用。"
        "仅当已有证据足够区分选项时填写，不能从草稿字母倒推事实。"
        "近义、包含关系或粒度不同不自动构成映射错误；有歧义则不强行改字母。\n"
        f"原始选项：{[o.model_dump(mode='json') for o in request.choice_options]}\n"
        "slot_assessments：对每个 answer slot 保留 claim_refs；序列题的 required_event_ids "
        "必须列齐题目全部事件，不能只排列已发现的两个事件。前后比较填 required_roles "
        "before/after，各端点使用各自的 event_instance 和 claim_ref。first 先定位锚点，"
        "required_ranges 从全片起点到锚点；after/before 必须保留中间未核验空档。"
        "绝对时间只能取 evidence_facts 的 source/locator，不把片段相对秒数当全片时间。"
        "人物跨场景允许换装；身份由脸、声音或连续动作确认，不能仅凭衣色认同或排除。"
        "口头提及问题的 required_modalities 包含 asr，字幕可作语音证据，国旗地图不能。"
        "冲突 claims 保留 conflicting_claim_refs 和 missing_conditions，不能投票消除。\n"
        f"answer slots：{json.dumps(request.answer_slots, ensure_ascii=False)}\n"
        "已记录证据（引用与时间的权威来源）："
        f"{json.dumps(request.evidence_facts, ensure_ascii=False)}\n"
        "内容自洽 contradiction 规则：\n"
        "- 检查答案内部 claim 是否互相矛盾。\n"
        "- 特别关注步骤编号、阶段编号、子步骤编号是否混用。\n"
        "- 特别关注同一对象的数量、位置、时间、状态是否前后不一致。\n"
        "- 开头结论、展开说明和总结句冲突时应降低 contradiction_score。\n\n"
        "意图覆盖 coverage 规则：\n"
        "- 把用户问题拆成必要的 intent units。\n"
        "- 判断每个 intent unit 是 answered、partial 还是 missing。\n"
        "- 检查关键约束是否满足，例如第几步、哪个对象、只回答某范围。\n"
        "- 答案只给背景、知识库概要或无关扩展但没有直接回答时，应降低 coverage_score。\n\n"
        "补救：每个需要额外证据的风险填写 slot_ids、missing_evidence 或 "
        "conflicting_claim_refs，以及 repair_action=inspect/locate_adjacent。"
        "只需修改字母、引用或已有事实表述时用 revise_answer，不要求重新检索。"
        "缺少比较端点、时间空档、未读清的手部/材质/文字必须具体指出，"
        "不能把同窗重问当作独立确认。\n"
        "输出约束：\n"
        "- contradiction_score 和 coverage_score 必须是 0 到 100。\n"
        "- conflicts 只记录答案内部冲突，不记录答案与 citation evidence 的支撑问题。\n"
        "- intent_units 必须记录用户问题中的核心子目标及覆盖状态。\n"
        "- risks 可使用 numbering_conflict、internal_contradiction、missing_intent_unit、"
        "partial_intent_coverage、over_answering、poor_refusal_quality 等 kind。\n\n"
        f"问题：{request.question}\n"
        f"答案：{request.answer}\n"
        f"引用策略：{request.citation_policy.model_dump(mode='json')}\n"
        f"answer spans：{json.dumps(span_payload, ensure_ascii=False)}\n"
        f"answer claims：{json.dumps(claim_payload, ensure_ascii=False)}\n"
        f"risk severity 只能使用：{[item.value for item in RiskSeverity]}\n"
    )


__all__ = [
    "LLMClaimReviewer",
    "LLMPolicyClassifier",
    "LLMSemanticReviewer",
]
