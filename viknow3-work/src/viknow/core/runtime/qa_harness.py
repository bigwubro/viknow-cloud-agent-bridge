"""Read-only QA plan context for the existing answer/citation judge."""

from viknow.core.runtime.answer_plan import RunAnswerPlan
from viknow.core.runtime.answer_plan_tool import RecordedAnswerPlanReview, evidence_digest
from viknow.core.runtime.evidence_ledger import RunEvidenceLedger


def answer_plan_judge_context(
    plan: RunAnswerPlan | None,
    ledger: RunEvidenceLedger,
    review: RecordedAnswerPlanReview | None,
) -> dict | None:
    """Expose facts and agent assessment, never a second publication decision."""
    if plan is None:
        return None
    return {
        **plan.prompt_summary(),
        "agent_review": review.assessment.model_dump(mode="json") if review else None,
        "review_is_current": (
            review is not None
            and review.revision == plan.revision
            and review.evidence_digest == evidence_digest(ledger)
        ),
        "review_authority": "agent_assessment_only",
    }
