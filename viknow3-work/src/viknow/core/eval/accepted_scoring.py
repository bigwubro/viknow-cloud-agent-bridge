"""Score one trace against human-accepted Oracle facts."""

from __future__ import annotations

from typing import Any

from viknow.core.eval.evidence_oracle import EvidenceOraclePackage
from viknow.core.eval.quality_score import (
    OracleRequirementAssessment,
    OracleRequirementJudge,
    OracleRequirementJudgeRequest,
)
from viknow.core.eval.skill_flow import RefusalJudge, SkillEvalCase, SkillEvalResult, score_case


def score_accepted_trace(
    *,
    case: SkillEvalCase,
    trace: Any,
    oracle_package: EvidenceOraclePackage,
    judge: OracleRequirementJudge,
    refusal_judge: RefusalJudge | None = None,
) -> SkillEvalResult:
    """Apply the shared semantic judge and deterministic process score once."""

    if oracle_package.case_id != case.case_id:
        raise ValueError("accepted Oracle case_id differs from evaluation case")
    preliminary = score_case(
        case,
        trace,
        oracle_package=oracle_package,
        refusal_judge=refusal_judge,
    )
    assessments = assess_oracle_requirements(
        case=case,
        answer=preliminary.final_answer or "",
        oracle_package=oracle_package,
        inspect_funnel=preliminary.inspect_funnel_result,
        judge=judge,
    )
    return score_case(
        case,
        trace,
        oracle_package=oracle_package,
        oracle_assessments=assessments,
        quality_judge_model=judge.model,
        refusal_judge=refusal_judge,
    )


def assess_oracle_requirements(
    *,
    case: SkillEvalCase,
    answer: str,
    oracle_package: EvidenceOraclePackage,
    inspect_funnel: dict[str, Any],
    judge: OracleRequirementJudge,
) -> list[OracleRequirementAssessment]:
    """Judge supported requirements and explicitly preserve evidence gaps."""

    assessable = [
        requirement
        for requirement in oracle_package.requirements
        if requirement.disposition == "supports" and requirement.accepted_facts
    ]
    assessed = (
        judge.evaluate(
            OracleRequirementJudgeRequest(
                case_id=case.case_id,
                question=case.question,
                answer=answer,
                requirements=[
                    {
                        "requirement_id": requirement.requirement_id,
                        "accepted_facts": [
                            fact.model_dump() for fact in requirement.accepted_facts
                        ],
                    }
                    for requirement in assessable
                ],
            )
        )
        if assessable
        else []
    )
    citation_supported_ids = {
        str(item.get("gold_id") or "")
        for item in inspect_funnel.get("items") or []
        if isinstance(item, dict) and item.get("final_citation_matched")
    }
    assessed_by_id = {item.requirement_id: item for item in assessed}
    normalized: list[OracleRequirementAssessment] = []
    for requirement in oracle_package.requirements:
        assessment = assessed_by_id.get(requirement.requirement_id)
        if assessment is None:
            normalized.append(
                OracleRequirementAssessment(
                    requirement_id=requirement.requirement_id,
                    status="not_evaluable",
                    reason="oracle_requirement_has_no_accepted_fact",
                    confidence=1.0,
                )
            )
            continue
        normalized.append(
            assessment.model_copy(
                update={
                    "citation_supported": requirement.requirement_id in citation_supported_ids,
                }
            )
        )
    return normalized
