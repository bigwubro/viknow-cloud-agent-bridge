"""Translate existing review risks into a bounded evidence repair request."""

from __future__ import annotations

import json

from viknow3.agents.runtimes.deepagents.core.answer_review.domain import AnswerReviewResult, RiskSeverity


def evidence_repair_risks(review: AnswerReviewResult) -> list[dict]:
    return [
        risk.model_dump(mode="json")
        for risk in review.risks
        if risk.severity != RiskSeverity.LOW
        and risk.repair_action in {"inspect", "locate_adjacent"}
        and (risk.missing_evidence or risk.conflicting_claim_refs)
    ]


def targeted_retry_prompt(question: str, answer: str, risks: list[dict]) -> str:
    return (
        "Original task (unchanged):\n"
        + question
        + "\nPrevious draft (not new evidence):\n"
        + answer
        + "\nRepair only these evidence gaps, using the original run budget:\n"
        + json.dumps(risks, ensure_ascii=False)
        + "\nThis is the only supplementary round. Reuse verified claims and exact candidate "
        "locators. For missing adjacent windows use list_video_segments then inspect those "
        "legal windows separately. Do not restart discovery, query refinement or broad search. "
        "Inspect each comparison endpoint separately; preserve source and absolute time. "
        "Use existing AVI crop/fps/refinement for the specified unreadable region. "
        "Do not treat repeated questions over the same frames as independent evidence. "
        "If a gap remains, label it unverified and still choose the most likely original "
        "option as requested. Never invent citations, claims, or completed verification."
    )
