"""Structured, non-authoritative state for scoped Oracle research."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow3.agents.runtimes.deepagents.core.eval.evidence_oracle import EvidenceOracleScope

ResearchOutcomeStatus = Literal["completed", "research_incomplete", "no_evidence_in_scope"]
ResearchSeedSource = Literal["qa_citation", "discovery", "catalog", "adjacent"]
RequirementResearchStatus = Literal[
    "investigating",
    "supported",
    "contradicted",
    "unresolved",
    "no_evidence_in_scope",
]
ResearchEventStatus = Literal["candidate", "verified", "excluded", "unresolved"]
ScoutTaskPhase = Literal["catalog_mapping", "targeted_research"]
ResearchHypothesisStatus = Literal["supported", "challenged", "open"]
ExclusionDisposition = Literal["accepted", "reopen"]
InspectionDisposition = Literal["supports", "contradicts", "unknown"]
InspectionReportStatus = Literal["complete", "incomplete"]


class ResearchPlanSubquestion(BaseModel):
    """One answer requirement with the evidence checks that make it covered."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    coverage_condition: str = Field(min_length=1)
    coverage_checks: list[str] = Field(min_length=1)
    evidence_angles: list[str] = Field(min_length=1)
    requires_exhaustive_event_coverage: bool = False


class ResearchPlan(BaseModel):
    """The non-authoritative investigation plan for one Gate-A task."""

    model_config = ConfigDict(extra="forbid")

    subquestions: list[ResearchPlanSubquestion] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_requirement_ids(self) -> ResearchPlan:
        ids = [item.id for item in self.subquestions]
        if len(set(ids)) != len(ids):
            raise ValueError("research plan subquestion ids must be unique")
        return self


class RequirementProgress(BaseModel):
    """The mutable, auditable investigation state for one plan subquestion."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1)
    status: RequirementResearchStatus
    completed_checks: list[str] = Field(default_factory=list)
    unresolved_checks: list[str] = Field(default_factory=list)
    inspected_frontier_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_research_action: str | None = None
    rationale: str | None = None


class EventGroupingDecision(BaseModel):
    """Main-Agent semantic grouping over frontier facts already held by the runtime."""

    model_config = ConfigDict(extra="forbid")

    requirement_ids: list[str] = Field(min_length=1)
    frontier_ids: list[str] = Field(min_length=1)
    verification_targets: list[str] = Field(min_length=1)
    rationale: str | None = None


class ResearchEvent(BaseModel):
    """A candidate semantic event owned and adjudicated by the main Oracle agent."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    frontier_ids: list[str] = Field(min_length=1)
    time_range: dict[str, float] | None = None
    locators: list[dict[str, Any]] = Field(default_factory=list)
    verification_targets: list[str] = Field(min_length=1)
    status: ResearchEventStatus = "candidate"
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> ResearchEvent:
        if self.time_range is None:
            return self
        start = self.time_range.get("start_seconds")
        end = self.time_range.get("end_seconds")
        if start is None or end is None or end <= start:
            raise ValueError("event time_range requires increasing start_seconds and end_seconds")
        return self


class InspectionTask(BaseModel):
    """A bounded fact-verification handoff from the main agent to an Inspector."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    frontier_ids: list[str] = Field(min_length=1)
    time_range: dict[str, float] | None = None
    locators: list[dict[str, Any]] = Field(default_factory=list)
    verification_targets: list[str] = Field(min_length=1)
    decision_use: str = Field(min_length=1)
    prior_report_task_call_ids: list[str] = Field(default_factory=list)
    challenge: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> InspectionTask:
        if self.time_range is not None:
            start = self.time_range.get("start_seconds")
            end = self.time_range.get("end_seconds")
            if start is None or end is None or end <= start:
                raise ValueError(
                    "inspection time_range requires increasing start_seconds and end_seconds"
                )
        has_prior_reports = bool(self.prior_report_task_call_ids)
        has_challenge = bool(self.challenge and self.challenge.strip())
        if has_prior_reports != has_challenge:
            raise ValueError(
                "inspection reinvestigation requires both prior report references and a challenge"
            )
        if len(set(self.prior_report_task_call_ids)) != len(self.prior_report_task_call_ids):
            raise ValueError("prior report references must be unique")
        return self


class InspectionCallFact(BaseModel):
    """One actual inspect-tool execution observed by the Oracle harness."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    success: bool
    claim_refs: list[str] = Field(default_factory=list)
    error_code: str | None = None


class InspectionFindingDecision(BaseModel):
    """Inspector judgment for one verification target in its bounded task."""

    model_config = ConfigDict(extra="forbid")

    verification_target: str = Field(min_length=1)
    disposition: InspectionDisposition
    observed_facts: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class InspectionReport(BaseModel):
    """Canonical handoff combining a task, actual calls, and Inspector decisions."""

    model_config = ConfigDict(extra="forbid")

    task_call_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    frontier_ids: list[str] = Field(min_length=1)
    time_range: dict[str, float] | None = None
    locators: list[dict[str, Any]] = Field(min_length=1)
    verification_targets: list[str] = Field(min_length=1)
    inspection_calls: list[InspectionCallFact] = Field(min_length=1)
    status: InspectionReportStatus
    findings: list[InspectionFindingDecision] = Field(min_length=1)
    coverage_gaps: list[str] = Field(default_factory=list)
    prior_report_task_call_ids: list[str] = Field(default_factory=list)
    challenge: str | None = None


def build_inspection_report(
    *,
    task_call_id: str,
    inspection_task: InspectionTask,
    inspection_calls: list[InspectionCallFact],
    status: InspectionReportStatus,
    findings: list[InspectionFindingDecision],
    coverage_gaps: list[str],
) -> InspectionReport:
    """Bind Inspector-authored decisions to the task and calls observed by the harness."""

    expected_targets = set(inspection_task.verification_targets)
    finding_targets = [finding.verification_target for finding in findings]
    duplicate_targets = sorted(
        target for target, count in Counter(finding_targets).items() if count > 1
    )
    missing_targets = sorted(expected_targets - set(finding_targets))
    unknown_targets = sorted(set(finding_targets) - expected_targets)
    problems: list[str] = []
    if duplicate_targets:
        problems.append("duplicate verification targets: " + ", ".join(duplicate_targets))
    if missing_targets:
        problems.append("missing verification targets: " + ", ".join(missing_targets))
    if unknown_targets:
        problems.append("unknown verification targets: " + ", ".join(unknown_targets))
    if not inspection_calls:
        problems.append("inspection report requires an actual inspect-tool call")
    if status == "complete" and coverage_gaps:
        problems.append("complete inspection report cannot retain coverage gaps")
    if status == "complete" and any(finding.disposition == "unknown" for finding in findings):
        problems.append("complete inspection report cannot retain unknown findings")
    if status == "incomplete" and not coverage_gaps:
        problems.append("incomplete inspection report requires coverage gaps")
    if problems:
        raise ValueError("; ".join(problems))
    return InspectionReport(
        task_call_id=task_call_id,
        event_id=inspection_task.event_id,
        requirement_ids=inspection_task.requirement_ids,
        frontier_ids=inspection_task.frontier_ids,
        time_range=inspection_task.time_range,
        locators=inspection_task.locators,
        verification_targets=inspection_task.verification_targets,
        inspection_calls=inspection_calls,
        status=status,
        findings=findings,
        coverage_gaps=coverage_gaps,
        prior_report_task_call_ids=inspection_task.prior_report_task_call_ids,
        challenge=inspection_task.challenge,
    )


class ScoutTask(BaseModel):
    """A bounded, state-derived handoff for one Oracle Scout delegation.

    Catalog mapping is controlled by the runtime so every page is consumed
    under the same high-recall contract.  Later research is intentionally
    authored by the main agent and must name the new question it is trying to
    resolve, rather than repeating the original user question.
    """

    model_config = ConfigDict(extra="forbid")

    phase: ScoutTaskPhase
    research_round: int = Field(ge=1)
    original_question: str = Field(min_length=1)
    scope: dict[str, Any] = Field(default_factory=dict)
    unresolved_requirement_ids: list[str] = Field(min_length=1)
    candidate_policy: Literal["high_recall", "challenge"]
    objective: str = Field(min_length=1)
    catalog_id: str | None = None
    cursor: str | None = None
    page_size: int | None = Field(default=None, ge=1, le=40)
    challenge: str | None = None
    reflection_id: str | None = None
    prior_events: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_phase_contract(self) -> ScoutTask:
        if self.phase == "catalog_mapping":
            if self.candidate_policy != "high_recall":
                raise ValueError("catalog mapping requires high_recall candidate policy")
            if self.cursor is None or self.page_size is None:
                raise ValueError("catalog mapping requires cursor and page_size")
        elif self.candidate_policy != "challenge" or not self.challenge or not self.reflection_id:
            raise ValueError("targeted research requires a reflection-backed challenge contract")
        return self


class ResearchState(BaseModel):
    """Plan plus one current progress record for every immutable requirement."""

    model_config = ConfigDict(extra="forbid")

    plan: ResearchPlan
    progress: list[RequirementProgress] = Field(min_length=1)
    events: list[ResearchEvent] = Field(default_factory=list)
    inspection_reports: list[InspectionReport] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_progress_matches_plan(self) -> ResearchState:
        plan_ids = {item.id for item in self.plan.subquestions}
        progress_ids = [item.requirement_id for item in self.progress]
        if set(progress_ids) != plan_ids or len(progress_ids) != len(set(progress_ids)):
            raise ValueError("progress requirement ids must match ResearchPlan slot ids")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("research event ids must be unique")
        if any(
            requirement_id not in plan_ids
            for event in self.events
            for requirement_id in event.requirement_ids
        ):
            raise ValueError("research event requirement ids must belong to ResearchPlan")
        report_task_ids = [report.task_call_id for report in self.inspection_reports]
        if len(report_task_ids) != len(set(report_task_ids)):
            raise ValueError("inspection report task call ids must be unique")
        if any(
            requirement_id not in plan_ids
            for report in self.inspection_reports
            for requirement_id in report.requirement_ids
        ):
            raise ValueError("inspection report requirement ids must belong to ResearchPlan")
        if self.events and any(
            report.event_id not in set(event_ids) for report in self.inspection_reports
        ):
            raise ValueError("inspection report event ids must belong to the Event Map")
        events_by_id = {event.event_id: event for event in self.events}
        latest_reports_by_event: dict[str, InspectionReport] = {}
        for report in self.inspection_reports:
            latest_reports_by_event[report.event_id] = report
        uninspected_verified = [
            event.event_id
            for event in self.events
            if event.status == "verified"
            and (
                (report := latest_reports_by_event.get(event.event_id)) is None
                or not any(call.success for call in report.inspection_calls)
            )
        ]
        if uninspected_verified:
            raise ValueError(
                "verified events require a corresponding successful inspect in the latest "
                "canonical Inspector report: "
                + ", ".join(uninspected_verified)
                + ". Keep these events candidate/unresolved; reuse existing successful "
                "inspection handoffs or delegate the missing inspection before updating progress."
            )
        candidate_reported_events = sorted(
            event_id
            for event_id in latest_reports_by_event
            if events_by_id[event_id].status == "candidate"
        )
        incomplete_misclassified_events = sorted(
            event_id
            for event_id, report in latest_reports_by_event.items()
            if report.status == "incomplete" and events_by_id[event_id].status != "unresolved"
        )
        if candidate_reported_events or incomplete_misclassified_events:
            problems: list[str] = []
            if candidate_reported_events:
                problems.append(
                    "reported events must be adjudicated instead of remaining candidate: "
                    + ", ".join(candidate_reported_events)
                )
            if incomplete_misclassified_events:
                problems.append(
                    "events with an incomplete latest report must be unresolved: "
                    + ", ".join(incomplete_misclassified_events)
                )
            raise ValueError("; ".join(problems))
        return self


class ResearchHypothesis(BaseModel):
    """A main-Agent interpretation that the next research step can challenge."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    status: ResearchHypothesisStatus
    basis_refs: list[str] = Field(min_length=1)


class ResearchReflection(BaseModel):
    """A non-authoritative reasoning checkpoint after factual research progress.

    This preserves how the main agent interpreted the current factual ledger,
    without turning a task-specific semantic judgment into a runtime rule.
    """

    model_config = ConfigDict(extra="forbid")

    reflection_id: str = Field(min_length=1)
    hypotheses: list[ResearchHypothesis] = Field(min_length=1)
    counterexamples: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_challenge: str | None = None
    based_on_progress: ResearchState

    @model_validator(mode="after")
    def validate_open_hypothesis_has_next_challenge(self) -> ResearchReflection:
        if any(item.status == "open" for item in self.hypotheses) and not self.next_challenge:
            raise ValueError("open hypotheses require next_challenge")
        return self


class ExclusionReviewItem(BaseModel):
    """A main-Agent decision about one Scout exclusion relevant to a plan slot."""

    model_config = ConfigDict(extra="forbid")

    frontier_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    locator: dict[str, Any]
    scout_reason: str = Field(min_length=1)
    main_disposition: ExclusionDisposition
    rationale: str = Field(min_length=1)
    challenge: str | None = None

    @model_validator(mode="after")
    def validate_reopen_has_challenge(self) -> ExclusionReviewItem:
        if self.main_disposition == "reopen" and not self.challenge:
            raise ValueError("reopened exclusions require challenge")
        return self


class ExclusionReview(BaseModel):
    """Main-Agent audit of material Scout exclusions for exhaustive research."""

    model_config = ConfigDict(extra="forbid")

    research_plan: ResearchPlan
    reviews: list[ExclusionReviewItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_review_requirement_ids(self) -> ExclusionReview:
        plan_ids = {item.id for item in self.research_plan.subquestions}
        reviewed_ids = [item.frontier_id for item in self.reviews]
        if len(reviewed_ids) != len(set(reviewed_ids)):
            raise ValueError("exclusion review frontier ids must be unique")
        if any(
            requirement_id not in plan_ids
            for item in self.reviews
            for requirement_id in item.requirement_ids
        ):
            raise ValueError("exclusion review requirement ids must belong to ResearchPlan")
        return self


class ScoutMaterialExclusion(BaseModel):
    """A Scout locator deliberately withheld from the candidate frontier."""

    model_config = ConfigDict(extra="forbid")

    frontier_id: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    locator: dict[str, Any]
    scout_reason: str = Field(min_length=1)
    why_material: str = Field(min_length=1)


class ScoutSegmentDecision(BaseModel):
    """One Scout-authored semantic classification for one real page segment."""

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1)
    classification: Literal["candidate", "uncertain", "excluded"]
    requirement_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    match_sources: list[Literal["caption", "asr", "adjacent"]] = Field(default_factory=list)
    match_excerpt: str | None = None
    material_exclusion: bool = False
    why_material: str | None = None

    @model_validator(mode="after")
    def validate_material_exclusion(self) -> ScoutSegmentDecision:
        if self.material_exclusion and self.classification != "excluded":
            raise ValueError("material exclusion must classify the segment as excluded")
        if self.material_exclusion and not self.why_material:
            raise ValueError("material exclusion requires why_material")
        return self


class ScoutReport(BaseModel):
    """Structured, page-bounded Scout handoff retained as Oracle run fact."""

    model_config = ConfigDict(extra="forbid")

    research_plan: ResearchPlan
    catalog_id: str = Field(min_length=1)
    shard_cursor: str = Field(min_length=1)
    next_cursor: str | None = None
    # ``page_segment_ids`` is injected by the runtime from the actual page
    # result.  The three classifications are authored by the Scout.  Keeping
    # both makes a silent catalog omission detectable without encoding any
    # question- or asset-specific relevance heuristic.
    page_segment_ids: list[str] = Field(min_length=1)
    candidate_segment_ids: list[str] = Field(default_factory=list)
    uncertain_segment_ids: list[str] = Field(default_factory=list)
    excluded_segment_ids: list[str] = Field(default_factory=list)
    locators: list[EvidenceFrontierItem] = Field(default_factory=list)
    material_exclusions: list[ScoutMaterialExclusion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_report_references(self) -> ScoutReport:
        plan_ids = {item.id for item in self.research_plan.subquestions}
        frontier_ids = [item.frontier_id for item in self.locators]
        exclusion_ids = [item.frontier_id for item in self.material_exclusions]
        if len(frontier_ids) != len(set(frontier_ids)):
            raise ValueError("Scout report locator frontier ids must be unique")
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise ValueError("Scout report exclusion frontier ids must be unique")
        if set(frontier_ids) & set(exclusion_ids):
            raise ValueError("Scout report locators and exclusions must not overlap")
        page_ids = self.page_segment_ids
        classifications = [
            *self.candidate_segment_ids,
            *self.uncertain_segment_ids,
            *self.excluded_segment_ids,
        ]
        if len(set(page_ids)) != len(page_ids):
            raise ValueError("Scout report page segment ids must be unique")
        if len(set(classifications)) != len(classifications):
            raise ValueError("Scout report segment partition must not overlap")
        if set(classifications) != set(page_ids):
            raise ValueError("Scout report segment partition must cover the current page")
        retained_segment_ids = set(self.candidate_segment_ids) | set(self.uncertain_segment_ids)
        locator_segment_ids = {
            _locator_segment_id(item.locator)
            for item in self.locators
            if _locator_segment_id(item.locator) is not None
        }
        if retained_segment_ids - locator_segment_ids:
            raise ValueError("Scout report requires a locator for every retained segment")
        if locator_segment_ids - retained_segment_ids:
            raise ValueError("Scout report locators must only reference retained segments")
        exclusion_segment_ids = {
            _locator_segment_id(item.locator)
            for item in self.material_exclusions
            if _locator_segment_id(item.locator) is not None
        }
        if len(exclusion_segment_ids) != len(self.material_exclusions):
            raise ValueError("Scout report material exclusions require a segment_id locator")
        if exclusion_segment_ids - set(self.excluded_segment_ids):
            raise ValueError("Scout report material exclusions must reference excluded segments")
        requirement_ids = [
            requirement_id
            for item in [*self.locators, *self.material_exclusions]
            for requirement_id in item.requirement_ids
        ]
        if any(requirement_id not in plan_ids for requirement_id in requirement_ids):
            raise ValueError("Scout report requirement ids must belong to ResearchPlan")
        return self


def _locator_segment_id(locator: dict[str, Any]) -> str | None:
    segment_id = str(locator.get("segment_id") or "").strip()
    return segment_id or None


class ResearchSeed(BaseModel):
    """A non-authoritative locator that an Oracle must inspect again."""

    model_config = ConfigDict(extra="forbid")

    source: ResearchSeedSource
    requirement_ids: list[str] = Field(min_length=1)
    locator: dict[str, Any]
    note: str | None = None


class EvidenceFrontierItem(BaseModel):
    """A scoped locator selected for a later exact inspection."""

    model_config = ConfigDict(extra="forbid")

    frontier_id: str = Field(min_length=1)
    source: ResearchSeedSource
    requirement_ids: list[str] = Field(min_length=1)
    locator: dict[str, Any]
    research_round: int = Field(ge=1)
    selection_reason: str = Field(min_length=1)


def build_scout_report(
    *,
    research_plan: ResearchPlan,
    catalog_id: str,
    shard_cursor: str,
    next_cursor: str | None,
    library_id: str,
    asset_id: str,
    page_segments: list[dict[str, Any]],
    decisions: list[ScoutSegmentDecision],
    research_round: int,
) -> ScoutReport:
    """Build the redundant report shape from one semantic decision per segment."""

    page_ids = [str(segment.get("segment_id") or "").strip() for segment in page_segments]
    decision_ids = [decision.segment_id for decision in decisions]
    counts = Counter(decision_ids)
    duplicate_ids = sorted(segment_id for segment_id, count in counts.items() if count > 1)
    page_id_set = set(page_ids)
    decision_id_set = set(decision_ids)
    unknown_ids = sorted(decision_id_set - page_id_set)
    missing_ids = sorted(page_id_set - decision_id_set)
    plan_ids = {subquestion.id for subquestion in research_plan.subquestions}
    unknown_requirement_ids = sorted(
        {
            requirement_id
            for decision in decisions
            for requirement_id in decision.requirement_ids
            if requirement_id not in plan_ids
        }
    )
    problems: list[str] = []
    if duplicate_ids:
        problems.append(f"duplicate segment decisions: {', '.join(duplicate_ids)}")
    if unknown_ids:
        problems.append(f"unknown segment decisions: {', '.join(unknown_ids)}")
    if missing_ids:
        problems.append(f"missing segment decisions: {', '.join(missing_ids)}")
    if unknown_requirement_ids:
        problems.append("unknown requirement ids: " + ", ".join(unknown_requirement_ids))
    if any(not segment_id for segment_id in page_ids):
        problems.append("page segments require segment_id")
    if len(page_ids) != len(page_id_set):
        problems.append("page segment ids must be unique")
    if problems:
        raise ValueError("; ".join(problems))

    segments_by_id = {str(segment["segment_id"]): segment for segment in page_segments}
    candidate_ids: list[str] = []
    uncertain_ids: list[str] = []
    excluded_ids: list[str] = []
    locators: list[EvidenceFrontierItem] = []
    material_exclusions: list[ScoutMaterialExclusion] = []
    for decision in decisions:
        segment = segments_by_id[decision.segment_id]
        if decision.classification == "candidate":
            candidate_ids.append(decision.segment_id)
        elif decision.classification == "uncertain":
            uncertain_ids.append(decision.segment_id)
        else:
            excluded_ids.append(decision.segment_id)

        locator: dict[str, Any] = {
            "library_id": library_id,
            "asset_id": asset_id,
            "segment_id": decision.segment_id,
        }
        time_range = segment.get("time_range")
        if isinstance(time_range, dict):
            locator["time_range"] = time_range
        if decision.match_sources:
            locator["match_sources"] = decision.match_sources
        if decision.match_excerpt:
            locator["match_excerpt"] = decision.match_excerpt

        if decision.classification in {"candidate", "uncertain"}:
            locators.append(
                EvidenceFrontierItem(
                    frontier_id=decision.segment_id,
                    source="catalog",
                    requirement_ids=decision.requirement_ids,
                    locator=locator,
                    research_round=research_round,
                    selection_reason=decision.reason,
                )
            )
        if decision.material_exclusion:
            material_exclusions.append(
                ScoutMaterialExclusion(
                    frontier_id=decision.segment_id,
                    requirement_ids=decision.requirement_ids,
                    locator=locator,
                    scout_reason=decision.reason,
                    why_material=decision.why_material or "",
                )
            )

    return ScoutReport(
        research_plan=research_plan,
        catalog_id=catalog_id,
        shard_cursor=shard_cursor,
        next_cursor=next_cursor,
        page_segment_ids=page_ids,
        candidate_segment_ids=candidate_ids,
        uncertain_segment_ids=uncertain_ids,
        excluded_segment_ids=excluded_ids,
        locators=locators,
        material_exclusions=material_exclusions,
    )


def build_event_map(
    *,
    research_plan: ResearchPlan,
    scout_reports: list[ScoutReport],
    decisions: list[EventGroupingDecision],
) -> list[ResearchEvent]:
    """Resolve semantic groupings against canonical Scout frontier facts."""

    frontier_by_id: dict[str, EvidenceFrontierItem] = {}
    for report in scout_reports:
        for frontier in report.locators:
            frontier_by_id[frontier.frontier_id] = frontier
        for exclusion in report.material_exclusions:
            frontier_by_id[exclusion.frontier_id] = EvidenceFrontierItem(
                frontier_id=exclusion.frontier_id,
                source="catalog",
                requirement_ids=exclusion.requirement_ids,
                locator=exclusion.locator,
                research_round=1,
                selection_reason=exclusion.scout_reason,
            )

    plan_ids = {item.id for item in research_plan.subquestions}
    requested_frontier_ids = {
        frontier_id for decision in decisions for frontier_id in decision.frontier_ids
    }
    requested_requirement_ids = {
        requirement_id for decision in decisions for requirement_id in decision.requirement_ids
    }
    unknown_frontier_ids = sorted(requested_frontier_ids - set(frontier_by_id))
    unknown_requirement_ids = sorted(requested_requirement_ids - plan_ids)
    problems: list[str] = []
    if unknown_frontier_ids:
        problems.append("unknown frontier ids: " + ", ".join(unknown_frontier_ids))
    if unknown_requirement_ids:
        problems.append("unknown requirement ids: " + ", ".join(unknown_requirement_ids))
    if problems:
        raise ValueError("; ".join(problems))

    events: list[ResearchEvent] = []
    for index, decision in enumerate(decisions, start=1):
        frontiers = [frontier_by_id[item] for item in decision.frontier_ids]
        locators = [frontier.locator for frontier in frontiers]
        time_ranges = [locator.get("time_range") for locator in locators]
        temporal_count = sum(isinstance(item, dict) for item in time_ranges)
        if temporal_count not in {0, len(time_ranges)}:
            raise ValueError(
                "event grouping cannot mix temporal and non-temporal frontiers: "
                + ", ".join(decision.frontier_ids)
            )
        event_time_range = None
        if temporal_count:
            starts = [
                float(item["start_seconds"]) for item in time_ranges if isinstance(item, dict)
            ]
            ends = [float(item["end_seconds"]) for item in time_ranges if isinstance(item, dict)]
            event_span_seconds = max(ends) - min(starts)
            if len(frontiers) > 1 and event_span_seconds > 30:
                raise ValueError(
                    f"event grouping {index} exceeds 30 seconds; split frontier_ids into "
                    "local inspection events: " + ", ".join(decision.frontier_ids)
                )
            event_time_range = {"start_seconds": min(starts), "end_seconds": max(ends)}
        events.append(
            ResearchEvent(
                event_id=f"event-{index:04d}",
                requirement_ids=decision.requirement_ids,
                frontier_ids=decision.frontier_ids,
                time_range=event_time_range,
                locators=locators,
                verification_targets=decision.verification_targets,
                rationale=decision.rationale,
            )
        )
    return events


class CoverageEntry(BaseModel):
    """Coverage checks and their completion state for one research requirement."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1)
    required_checks: list[str] = Field(min_length=1)
    completed_checks: list[str] = Field(default_factory=list)
    remaining_frontier_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_checks(self) -> CoverageEntry:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("coverage required_checks must be unique")
        if len(set(self.completed_checks)) != len(self.completed_checks):
            raise ValueError("coverage completed_checks must be unique")
        if set(self.completed_checks) - set(self.required_checks):
            raise ValueError("coverage completed_checks must be required")
        return self

    @property
    def is_complete(self) -> bool:
        return set(self.required_checks).issubset(self.completed_checks)


class ResearchOutcome(BaseModel):
    """A coverage terminal state; never an accepted Oracle fact by itself."""

    model_config = ConfigDict(extra="forbid")

    status: ResearchOutcomeStatus
    coverage: list[CoverageEntry] = Field(min_length=1)
    note: str | None = None
    event_state: ResearchState | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_terminal_coverage(self) -> ResearchOutcome:
        if self.status in {"completed", "no_evidence_in_scope"} and not all(
            entry.is_complete and not entry.remaining_frontier_ids for entry in self.coverage
        ):
            raise ValueError(f"{self.status} requires complete coverage")
        if self.status == "research_incomplete" and not any(
            not entry.is_complete or entry.remaining_frontier_ids for entry in self.coverage
        ):
            raise ValueError("research_incomplete requires an uncovered coverage gap")
        if self.status == "completed" and self.event_state is not None:
            exhaustive_ids = {
                item.id
                for item in self.event_state.plan.subquestions
                if item.requires_exhaustive_event_coverage
            }
            if exhaustive_ids and not any(
                exhaustive_ids.intersection(event.requirement_ids)
                for event in self.event_state.events
            ):
                raise ValueError(
                    "completed research for exhaustive requirements requires an event map"
                )
            unresolved = [
                event.event_id
                for event in self.event_state.events
                if exhaustive_ids.intersection(event.requirement_ids)
                and event.status not in {"verified", "excluded"}
            ]
            if unresolved:
                raise ValueError(
                    "completed research cannot retain unresolved events: " + ", ".join(unresolved)
                )
        return self


def validate_research_seed_scope(
    scope: EvidenceOracleScope,
    seeds: list[ResearchSeed],
) -> None:
    """Reject seed locators outside the human-approved Oracle scope."""

    identities: set[tuple[str, str, str]] = set()
    for seed in seeds:
        locator = seed.locator
        library_id = str(locator.get("library_id") or locator.get("workspace") or "")
        asset_id = str(locator.get("asset_id") or locator.get("document_id") or "")
        if library_id != scope.library_id or (scope.asset_ids and asset_id not in scope.asset_ids):
            raise ValueError("research seed is outside task scope")
        identity = (
            library_id,
            asset_id,
            str(locator.get("locator_id") or locator.get("segment_id") or ""),
        )
        if identity in identities:
            raise ValueError("research seeds must be unique")
        identities.add(identity)


def validate_frontier_scope(
    scope: EvidenceOracleScope,
    frontier: list[EvidenceFrontierItem],
) -> None:
    """Reject duplicate or out-of-scope inspection candidates."""

    seen_ids: set[str] = set()
    seen_locators: set[tuple[str, str, str]] = set()
    for item in frontier:
        if item.frontier_id in seen_ids:
            raise ValueError("research frontier ids must be unique")
        seen_ids.add(item.frontier_id)
        locator = item.locator
        library_id = str(locator.get("library_id") or locator.get("workspace") or "")
        asset_id = str(locator.get("asset_id") or locator.get("document_id") or "")
        if library_id != scope.library_id or (scope.asset_ids and asset_id not in scope.asset_ids):
            raise ValueError("research frontier is outside task scope")
        identity = (
            library_id,
            asset_id,
            str(locator.get("locator_id") or locator.get("segment_id") or ""),
        )
        if identity in seen_locators:
            raise ValueError("research frontier locators must be unique")
        seen_locators.add(identity)
