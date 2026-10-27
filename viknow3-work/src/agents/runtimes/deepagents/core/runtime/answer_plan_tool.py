"""One model-facing entry for agent-owned plans and non-authoritative reviews."""

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

from agents.runtimes.deepagents.core.runtime.answer_plan import (
    CLAIM_REF_INPUT_DESCRIPTION,
    AnswerPlanUpdate,
    AnswerSlotDefinition,
    RunAnswerPlan,
)
from agents.runtimes.deepagents.core.runtime.evidence_ledger import RunEvidenceLedger

if TYPE_CHECKING:
    from agents.runtimes.deepagents.core.tools.base import ToolResult


class AnswerPlanAssessmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_id: str = Field(min_length=1)
    assessment: str = Field(
        min_length=1,
        description=(
            "Assess the current interpretation against supporting and conflicting evidence; "
            "explain what was resolved, retained or changed and any remaining gap."
        ),
    )


class AnswerPlanUncertainty(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    issue: str = Field(min_length=1)
    possible_answers: list[str] = Field(min_length=2)


class AnswerPlanAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    items: list[AnswerPlanAssessmentItem] = Field(min_length=1)
    conclusion: Literal["answer_supported", "requested_fact_unanswerable"] = Field(
        description=(
            "Use answer_supported only when evidence supports a substantive answer to the "
            "requested fact. Use requested_fact_unanswerable when the requested fact cannot "
            "be determined; supplying related or conditional information is not an answer."
        )
    )
    conclusion_reason: str = Field(min_length=1, description="Brief evidence-based reason.")
    original_question_check: str = Field(
        min_length=1,
        description=(
            "Compare the intended answer with the user's original question and clarifications, "
            "not just the revised goals. Explain any changed interpretation and whether it "
            "still answers the requested scope; distinguish your assumptions "
            "from user requirements."
        ),
    )
    limitations: list[str] = Field(
        default_factory=list,
        description=(
            "For each unresolved gap, explain how it limits the conclusion you can give. "
            "Distinguish a supported partial finding from an established total or absence."
        ),
    )
    uncertainties: list[AnswerPlanUncertainty] = Field(
        description=(
            "Required unresolved interpretation ambiguities found by comparing the original "
            "question with current evidence. For each ambiguity, give every evidence-supported "
            "possible answer. Submit [] only when no material ambiguity remains. Record resolved "
            "interpretations and their basis in original_question_check instead."
        ),
    )


class AnswerPlanReviewItem(AnswerPlanAssessmentItem):
    """Stored review item; retain evidence references from existing checkpoints."""

    claim_refs: list[str] = Field(default_factory=list, description=CLAIM_REF_INPUT_DESCRIPTION)


class AnswerPlanReview(AnswerPlanAssessment):
    """Persisted assessment, distinct from the current model submission contract."""

    items: list[AnswerPlanReviewItem] = Field(min_length=1)
    uncertainties: list[AnswerPlanUncertainty] = Field(default_factory=list)

    def validate_against(
        self,
        plan: RunAnswerPlan,
        ledger: RunEvidenceLedger,
        *,
        historical_claim_refs: frozenset[str] = frozenset(),
    ) -> None:
        ids = [item.slot_id for item in self.items]
        if len(ids) != len(set(ids)) or set(ids) != {slot.id for slot in plan.slots}:
            raise ValueError("Review must assess each current goal exactly once.")
        available = {claim.claim_ref for claim in ledger.claims} | historical_claim_refs
        if any(ref not in available for item in self.items for ref in item.claim_refs):
            raise ValueError("Review references an unavailable claim.")


def _action_payload_schema(schema: dict) -> None:
    # Omission expresses an unused action payload. Advertising object|null caused
    # tested providers to emit JSON strings instead of objects. Keep the object
    # schema derived from its model and action-dependent validation at runtime.
    for action in ("update", "review", "content", "findings", "template_id"):
        schema["properties"].get(action, {}).pop("default", None)


class AnswerPlanTextFinding(BaseModel):
    """One conclusion in the knowledge-qa-v2 text plan."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)
    claim_refs: list[str] = Field(
        default_factory=list,
        description="Returned citation aliases for this conclusion only, such as [C1].",
    )


class AnswerPlanCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_action_payload_schema)

    action: Literal["view", "update", "review", "start"]
    content: str | SkipJsonSchema[None] = Field(
        default=None,
        max_length=6000,
        description=(
            "Complete current plan text for knowledge-qa-v2. "
            "Do not put citation aliases in content."
        ),
    )
    findings: list[AnswerPlanTextFinding] | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "Conclusions for knowledge-qa-v2. text stays clean; claim_refs hold the "
            "returned aliases. On update, omit to clear. On review, omit to keep."
        ),
    )
    template_id: Literal["generic", "comparison"] | SkipJsonSchema[None] = Field(
        default=None,
        description="Optional knowledge-qa-v2 template. Omit to keep the current choice.",
    )
    update: AnswerPlanUpdate | SkipJsonSchema[None] = Field(
        default=None, description="Legacy slot update object. Do not combine with content."
    )
    review: AnswerPlanAssessment | SkipJsonSchema[None] = Field(
        default=None,
        description="Legacy slot review object. Do not combine with content.",
    )

    @model_validator(mode="after")
    def validate_action(self):
        text_mode = (
            self.content is not None or self.findings is not None or self.template_id is not None
        )
        if text_mode and (self.update is not None or self.review is not None):
            raise ValueError("Do not combine content or findings with update or review objects.")
        if text_mode:
            if self.action == "view":
                raise ValueError("view omits content, findings, and template_id.")
            return self
        if (self.update is not None) != (self.action == "update"):
            raise ValueError("Only action=update requires an update payload.")
        if (self.review is not None) != (self.action == "review"):
            raise ValueError("Only action=review requires a review payload.")
        return self


def evidence_digest(ledger: RunEvidenceLedger) -> str:
    """Version evidence, not bookkeeping events caused by viewing/reviewing it."""
    payload = ledger.model_dump(mode="json", include={"claims", "candidate_manifests"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class AnswerPlanObservation(BaseModel):
    """Immutable run-local state at the model request that produced a review."""

    model_config = ConfigDict(frozen=True)
    run_id: str
    revision: int
    evidence_digest: str


class RecordedAnswerPlanReview(BaseModel):
    assessment: AnswerPlanReview
    revision: int
    evidence_digest: str


def answer_plan_error_message(error: Exception) -> str:
    if not isinstance(error, ValidationError):
        return str(error)
    issues = []
    for issue in error.errors(include_url=False, include_input=False):
        if issue["type"] == "none_required":
            continue
        path = ".".join(
            str(part)
            for part in issue["loc"]
            if isinstance(part, int)
            or not (
                str(part).startswith("function-")
                or str(part).startswith("list[")
                or str(part).startswith("Answer")
            )
        )
        issues.append(f"{path or 'command'}: {issue['msg']}")
    return "; ".join(dict.fromkeys(issues))


def answer_plan_error_result(error: Exception, *, current_plan: dict) -> "ToolResult":
    """Reject invalid input without hiding the contract needed to correct it.

    The schema is authoritative, not an executable repair or a second input API.
    Domain failures retain their specific explanation rather than requesting an
    unchanged retry. No original invalid values are echoed.
    """
    # tools.__init__ loads the registry, which imports this module's schema.
    # Resolve the envelope only when handling a call, after registration finishes.
    from agents.runtimes.deepagents.core.tools.base import ToolErrorCode, ToolResult

    return ToolResult.fail(
        "answer_plan",
        ToolErrorCode.INVALID_ARGUMENTS,
        answer_plan_error_message(error),
        data=current_plan,
        next_action=(
            {
                "tool": "answer_plan",
                "instruction": (
                    "Correct the reported field path against parameters_schema, then submit "
                    "a new call. Action payloads belong inside update or review; objects and "
                    "arrays must be JSON values, not encoded strings. The rejected call did "
                    "not change the plan. Decide the content yourself."
                ),
                "parameters_schema": AnswerPlanCommand.model_json_schema(),
            }
            if isinstance(error, ValidationError)
            else None
        ),
    )


ANSWER_PLAN_SCHEMA = {
    "description": (
        "Maintain the knowledge-qa-v2 answer plan. action is view, start, update, or review. "
        "Pass content as the complete current plan and findings as a JSON array of "
        "{text, claim_refs}. Do not pass update objects, slot_updates, slot_definitions, "
        "or status enums. view omits content and findings. start creates the plan. "
        "update replaces content and findings; omitted findings clears them. "
        "review records why investigation can end and does not generate the answer. "
        "Submit the answer afterward with final_check()."
    ),
    "parameters": AnswerPlanCommand.model_json_schema(),
}


def answer_plan_view(
    plan: RunAnswerPlan,
    ledger: RunEvidenceLedger,
    review: RecordedAnswerPlanReview | None,
    *,
    citations: dict[str, str],
) -> dict:
    """Project readable run evidence without copying it into persistent plan state."""
    return {
        "answer_plan": plan.prompt_summary(),
        "evidence": [
            {
                "claim_ref": claim.claim_ref,
                "claim": claim.claim,
                "status": claim.status.value,
                "eligibility_reason": claim.eligibility_reason,
                "citation": citations.get(claim.claim_ref),
                "tool_call_id": claim.tool_call_id,
                "tool_name": claim.tool_name,
                "locator": claim.locator.model_dump(mode="json") if claim.locator else None,
            }
            for claim in ledger.claims
        ],
        "review": (
            review.assessment.model_dump(
                mode="json", exclude={"items": {"__all__": {"claim_refs"}}}
            )
            if review
            else None
        ),
        "review_is_current": (
            review is not None
            and review.revision == plan.revision
            and review.evidence_digest == evidence_digest(ledger)
        ),
        "review_authority": "agent_assessment_only",
    }


def answer_plan_goal_changes(before: RunAnswerPlan, after: RunAnswerPlan) -> list[dict]:
    """Project applied definition changes, not a semantic verdict or another state owner."""
    fields = set(AnswerSlotDefinition.model_fields)
    previous = {slot.id: slot.model_dump(mode="json", include=fields) for slot in before.slots}
    current = {slot.id: slot.model_dump(mode="json", include=fields) for slot in after.slots}
    return [
        {"slot_id": slot_id, "before": previous.get(slot_id), "after": current.get(slot_id)}
        for slot_id in dict.fromkeys([*previous, *current])
        if previous.get(slot_id) != current.get(slot_id)
    ]
