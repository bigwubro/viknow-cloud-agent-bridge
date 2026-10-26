"""Run-scoped answer requirements for evidence-backed QA."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

_MAX_CANDIDATE_LOCATORS_PER_SLOT = 20

CLAIM_REF_INPUT_DESCRIPTION = (
    "Copy exact claim_ref values from inspect claims or answer_plan view evidence. "
    "These identify individual claims, not citation labels such as [1], "
    "chunk IDs, or tool-call IDs. An inspect_claim_refs entry must be verified; "
    "If no inspected claim supports the finding, leave it unresolved."
)


class AnswerCoverageRequirement(StrEnum):
    LOCAL = "local"
    FULL_ASSET = "full_asset"


class AnswerSlotStatus(StrEnum):
    PENDING = "pending"
    CANDIDATE_FOUND = "candidate_found"
    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    BLOCKED = "blocked"


class AnswerSlotRole(StrEnum):
    EVIDENCE = "evidence"
    COLLECTION = "collection"


class AnswerEvidenceLocator(BaseModel):
    """Content-free locator retained in the run plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    library_id: str = ""
    asset_id: str = ""
    evidence_id: str = ""
    segment_id: str = ""
    page: int | None = Field(default=None, ge=1)
    time_start: float | None = Field(default=None, ge=0)
    time_end: float | None = Field(default=None, ge=0)


class VerifiedAnswerItem(BaseModel):
    """One deduplicable answer unit supported by current-run inspect claims."""

    model_config = ConfigDict(extra="forbid")

    identity: str = Field(min_length=1)
    label: str = Field(min_length=1)
    inspect_claim_refs: list[str] = Field(min_length=1, description=CLAIM_REF_INPUT_DESCRIPTION)
    numeric_value: float | None = None
    quantity: int = Field(default=1, ge=1)


class AnswerSlotDefinition(BaseModel):
    """Investigation requirements, without evidence or completion assertions."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    role: AnswerSlotRole = AnswerSlotRole.EVIDENCE
    subquestion: str = Field(min_length=1)
    context: str | None = Field(
        default=None,
        description="Current interpretation of the goal and assumptions to check against evidence.",
    )
    required_modalities: list[str] = Field(
        default_factory=lambda: ["text"],
        description="Planner-suggested investigation modalities, not verification constraints.",
    )
    optional_modalities: list[str] = Field(default_factory=list)
    coverage_condition: str = Field(min_length=1)
    answer_unit: str = ""

    @model_validator(mode="after")
    def validate_role_contract(self) -> AnswerSlotDefinition:
        if self.role == AnswerSlotRole.COLLECTION and not self.answer_unit.strip():
            raise ValueError("collection answer slot requires answer_unit")
        return self


class AnswerSlotState(AnswerSlotDefinition):
    status: AnswerSlotStatus = AnswerSlotStatus.PENDING
    candidate_locators: list[AnswerEvidenceLocator] = Field(default_factory=list)
    inspect_claim_refs: list[str] = Field(default_factory=list)
    verified_items: list[VerifiedAnswerItem] = Field(default_factory=list)
    verified_value: float | None = None
    fact_identity: str = ""
    note: str = ""


class SlotPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="The exact id of an existing answer slot.")
    status: AnswerSlotStatus | SkipJsonSchema[None] = Field(
        default=None, description="Omit to retain the current status when recording progress."
    )
    candidate_locators: list[AnswerEvidenceLocator] = Field(default_factory=list)
    inspect_claim_refs: list[str] = Field(
        default_factory=list, description=CLAIM_REF_INPUT_DESCRIPTION
    )
    verified_items: list[VerifiedAnswerItem] = Field(
        default_factory=list,
        description=(
            "Distinct items only for slots with an existing answer_unit. "
            "For a single fact use inspect_claim_refs instead. "
            "Use slot_definitions in this tool to revise requirements if an item unit is needed."
        ),
    )
    verified_value: float | None = None
    fact_identity: str = ""
    note: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "Current supporting or conflicting observations, remaining gap and chosen next check. "
            "Omit to retain the previous note; an empty string clears it. Notes are not evidence."
        ),
    )

    @model_validator(mode="after")
    def require_progress(self) -> SlotPlanUpdate:
        if not (
            self.status is not None
            or self.note is not None
            or self.candidate_locators
            or self.inspect_claim_refs
            or self.verified_items
            or self.verified_value is not None
            or self.fact_identity
        ):
            raise ValueError("A slot update requires progress, such as a note or status.")
        return self


class CatalogSegmentDecision(BaseModel):
    """One semantic disposition for a segment exposed by a bounded catalog page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str = Field(min_length=1)
    disposition: Literal["candidate", "uncertain", "excluded"]
    slot_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class CatalogPageReview(BaseModel):
    """An exact partition of one previously observed catalog page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(min_length=1)
    cursor: str = ""
    decisions: list[CatalogSegmentDecision] = Field(min_length=1)


class AnswerRetrievalQuery(BaseModel):
    """An agent-adopted search binding; never a grant of library access."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    library_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    slot_ids: list[str] = Field(min_length=1)


class AnswerPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_updates: list[SlotPlanUpdate] = Field(
        default_factory=list,
        description="An array of update objects, not a JSON-encoded string.",
    )
    catalog_reviews: list[CatalogPageReview] = Field(default_factory=list)
    retrieval_queries: list[AnswerRetrievalQuery] | SkipJsonSchema[None] = Field(
        default=None,
        description="Optional replacement of adopted library/query/slot bindings; [] clears them.",
    )
    coverage_requirement: AnswerCoverageRequirement | SkipJsonSchema[None] = Field(
        default=None,
        description="Optional local/full_asset scope assessment; not a publication rule.",
    )
    slot_definitions: list[AnswerSlotDefinition] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional complete replacement of investigation goals, not progress updates. "
            "Include every goal to retain. Changed goals reset completion; unchanged goals keep "
            "their facts. Optional rationale explains the change; slot_updates may rebind claims. "
            "Omit when only recording progress. The original user question is unchanged."
        ),
    )
    rationale: str = Field(
        default="",
        description="Optional explanation inside update, not a prerequisite for saving it.",
    )

    @model_validator(mode="after")
    def require_an_update(self) -> AnswerPlanUpdate:
        if (
            not self.slot_updates
            and not self.catalog_reviews
            and self.slot_definitions is None
            and self.retrieval_queries is None
            and self.coverage_requirement is None
        ):
            raise ValueError(
                "answer plan update requires slot_updates or catalog_reviews or slot_definitions, "
                "retrieval_queries or coverage_requirement"
            )
        if self.slot_definitions is not None:
            ids = [slot.id for slot in self.slot_definitions]
            if len(ids) != len(set(ids)):
                raise ValueError("answer plan contains duplicate slot ids")
        return self


class CatalogScanRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)


class CatalogSegmentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1)
    cursor: str = ""
    page: int | None = Field(default=None, ge=1)
    time_start: float | None = Field(default=None, ge=0)
    time_end: float | None = Field(default=None, ge=0)
    disposition: Literal["candidate", "uncertain", "excluded"] | None = None
    slot_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    inspect_claim_refs: list[str] = Field(default_factory=list)


class AnswerPlanCatalogCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=1)
    library_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    total_segments: int = Field(ge=0)
    scanned_ranges: list[CatalogScanRange] = Field(default_factory=list)
    segments: list[CatalogSegmentState] = Field(default_factory=list)

    @property
    def listing_complete(self) -> bool:
        return self.total_segments == 0 or self.scanned_ranges == [
            CatalogScanRange(start_offset=0, end_offset=self.total_segments)
        ]

    @property
    def review_complete(self) -> bool:
        return (
            self.listing_complete
            and len(self.segments) == self.total_segments
            and all(segment.disposition is not None for segment in self.segments)
        )

    @property
    def unverified_retained_segment_ids(self) -> list[str]:
        return [
            segment.segment_id
            for segment in self.segments
            if segment.disposition in {"candidate", "uncertain"} and not segment.inspect_claim_refs
        ]

    @property
    def complete(self) -> bool:
        return self.review_complete and not self.unverified_retained_segment_ids

    @property
    def pending_review_pages(self) -> list[dict[str, Any]]:
        cursors = list(dict.fromkeys(segment.cursor for segment in self.segments))
        return [
            {
                "cursor": cursor,
                "segment_ids": [
                    segment.segment_id
                    for segment in self.segments
                    if segment.cursor == cursor and segment.disposition is None
                ],
            }
            for cursor in cursors
            if any(
                segment.cursor == cursor and segment.disposition is None
                for segment in self.segments
            )
        ]


class RunAnswerPlan(BaseModel):
    """A bounded, revisable contract between retrieval and final synthesis."""

    model_config = ConfigDict(extra="forbid")

    original_question: str = Field(min_length=1)
    is_initial_goal: bool = False
    coverage_requirement: AnswerCoverageRequirement = AnswerCoverageRequirement.LOCAL
    slots: list[AnswerSlotState] = Field(min_length=1)
    retrieval_queries: list[dict[str, Any]] = Field(default_factory=list)
    catalog_coverages: list[AnswerPlanCatalogCoverage] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    rationale: str = ""

    @classmethod
    def initial(cls, *, original_question: str) -> RunAnswerPlan:
        """Retain the user's goal before retrieval without guessing its complexity."""
        return cls(
            original_question=original_question,
            is_initial_goal=True,
            slots=[
                AnswerSlotState(
                    id="answer",
                    subquestion=original_question,
                    required_modalities=[],
                    coverage_condition=(
                        "回答原问题的全部要求；核对适用范围、必要组成项以及是否存在遗漏或反证，"
                        "以已核验证据支持结论，未解决部分明确说明限制。"
                    ),
                )
            ],
        )

    def revise_from_query_refine(self, payload: dict[str, Any]) -> RunAnswerPlan:
        """A changed requirement needs fresh assessment, not inherited completion."""
        revised = self.from_query_refine(original_question=self.original_question, payload=payload)
        self._retain_unchanged_work(revised)
        revised.revision = self.revision + 1
        return revised

    def _retain_unchanged_work(self, revised: RunAnswerPlan) -> set[str]:
        """Keep factual state only where the requirements still have the same meaning."""
        definition_fields = {
            "id",
            "role",
            "subquestion",
            "context",
            "coverage_condition",
            "answer_unit",
        }
        old_slots = {slot.id: slot for slot in self.slots}
        unchanged_ids: set[str] = set()
        for index, slot in enumerate(revised.slots):
            old = old_slots.get(slot.id)
            if old is not None and old.model_dump(include=definition_fields) == slot.model_dump(
                include=definition_fields
            ):
                revised.slots[index] = old.model_copy(
                    deep=True,
                    update={
                        "required_modalities": list(slot.required_modalities),
                        "optional_modalities": list(slot.optional_modalities),
                    },
                )
                unchanged_ids.add(slot.id)
        revised.catalog_coverages = [item.model_copy(deep=True) for item in self.catalog_coverages]
        if unchanged_ids != set(old_slots) or unchanged_ids != {slot.id for slot in revised.slots}:
            # Scanned page identities remain facts; exclusions and coverage are
            # judgments relative to the old requirements and must be reviewed.
            for coverage in revised.catalog_coverages:
                for segment in coverage.segments:
                    segment.disposition = None
                    segment.slot_ids = []
                    segment.reason = ""
                    segment.inspect_claim_refs = []
        return unchanged_ids

    @model_validator(mode="after")
    def validate_graph(self) -> RunAnswerPlan:
        slot_ids = [slot.id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("answer plan contains duplicate slot ids")
        return self

    @property
    def unresolved_slot_ids(self) -> list[str]:
        return [
            slot.id
            for slot in self.slots
            if slot.status not in {AnswerSlotStatus.VERIFIED, AnswerSlotStatus.NOT_FOUND}
        ]

    @property
    def source_coverage_required(self) -> bool:
        return self.coverage_requirement == AnswerCoverageRequirement.FULL_ASSET

    @property
    def source_coverage_complete(self) -> bool:
        if not self.source_coverage_required:
            return True
        return bool(self.catalog_coverages) and all(
            coverage.complete for coverage in self.catalog_coverages
        )

    @property
    def pending_catalog_review_pages(self) -> list[dict[str, Any]]:
        return [
            {"catalog_id": coverage.catalog_id, **page}
            for coverage in self.catalog_coverages
            for page in coverage.pending_review_pages
        ]

    def catalog_target(self) -> dict[str, str] | None:
        """Return one unambiguous asset target for required catalog coverage."""
        if not self.source_coverage_required:
            return None
        locators = [locator for slot in self.slots for locator in slot.candidate_locators]
        targets = {
            (locator.library_id, locator.asset_id)
            for locator in locators
            if locator.library_id and locator.asset_id
        }
        if len(targets) != 1:
            return None
        library_id, asset_id = next(iter(targets))
        video_required = any(
            modality in {"video", "asr"}
            for slot in self.slots
            for modality in slot.required_modalities
        )
        video_locator = any(
            locator.time_start is not None or locator.time_end is not None for locator in locators
        )
        return {
            "tool": (
                "list_video_segments" if video_required or video_locator else "list_doc_segments"
            ),
            "library_id": library_id,
            "asset_id": asset_id,
        }

    @classmethod
    def from_query_refine(
        cls,
        *,
        original_question: str,
        payload: dict[str, Any],
    ) -> RunAnswerPlan:
        slots = [AnswerSlotState.model_validate(item) for item in payload.get("answer_slots") or []]
        if not slots:
            raise ValueError("query_refine result does not contain answer_slots")
        return cls(
            original_question=original_question,
            coverage_requirement=payload.get("coverage_requirement") or "local",
            slots=slots,
            retrieval_queries=[
                dict(item)
                for item in payload.get("retrieval_queries") or []
                if isinstance(item, dict)
            ],
            rationale=str(payload.get("decomposition_reason") or payload.get("reasoning") or ""),
        )

    def apply_update(
        self,
        update: AnswerPlanUpdate,
        *,
        available_claim_refs: set[str],
        claim_segment_ids: dict[str, set[tuple[str, str]]] | None = None,
    ) -> RunAnswerPlan:
        revised = self.model_copy(deep=True)
        if update.slot_definitions is not None:
            revised.slots = [
                AnswerSlotState.model_validate(slot.model_dump())
                for slot in update.slot_definitions
            ]
            revised.is_initial_goal = False
            unchanged_ids = self._retain_unchanged_work(revised)
            revised.retrieval_queries = [
                {**query, "slot_ids": retained_ids}
                for query in revised.retrieval_queries
                if (
                    retained_ids := [
                        sid for sid in query.get("slot_ids", []) if sid in unchanged_ids
                    ]
                )
            ]
        by_id = {slot.id: slot for slot in revised.slots}
        if update.retrieval_queries is not None:
            for query in update.retrieval_queries:
                if set(query.slot_ids) - set(by_id):
                    raise ValueError("retrieval query references unknown answer slot(s)")
            revised.retrieval_queries = [query.model_dump() for query in update.retrieval_queries]
        if update.coverage_requirement is not None:
            revised.coverage_requirement = update.coverage_requirement
        for catalog_review in update.catalog_reviews:
            revised._apply_catalog_review(catalog_review, slot_ids=set(by_id))
        for slot_update in update.slot_updates:
            slot = by_id.get(slot_update.id)
            if slot is None:
                raise ValueError(f"unknown answer slot: {slot_update.id}")
            if slot_update.verified_items and not slot.answer_unit:
                raise ValueError(
                    f"answer slot {slot.id} does not declare an answer unit. "
                    "For a single fact, use inspect_claim_refs without verified_items. "
                    "For a collection, revise the slot's answer_unit using slot_definitions "
                    "in answer_plan (or request suggestions from query_refine) "
                    "before recording distinct items."
                )
            item_claim_refs = [
                claim_ref
                for item in slot_update.verified_items
                for claim_ref in item.inspect_claim_refs
            ]
            update_claim_refs = list(
                dict.fromkeys([*slot_update.inspect_claim_refs, *item_claim_refs])
            )
            unknown_claims = set(update_claim_refs) - available_claim_refs
            if unknown_claims:
                raise ValueError(
                    "unknown inspect claim ref(s): " + ", ".join(sorted(unknown_claims))
                )
            if slot_update.status == AnswerSlotStatus.VERIFIED and not update_claim_refs:
                raise ValueError("verified answer slot requires at least one inspect claim ref")
            if slot_update.status is not None:
                slot.status = slot_update.status
            slot.candidate_locators = _deduplicate_locators(
                [*slot.candidate_locators, *slot_update.candidate_locators]
            )
            slot.inspect_claim_refs = list(
                dict.fromkeys([*slot.inspect_claim_refs, *update_claim_refs])
            )
            slot.verified_items = _merge_verified_items(
                slot.verified_items,
                slot_update.verified_items,
            )
            if slot_update.note is not None:
                slot.note = slot_update.note
            if slot_update.verified_value is not None:
                slot.verified_value = slot_update.verified_value
            if slot_update.fact_identity:
                slot.fact_identity = slot_update.fact_identity
        # Coverage is derived from the current slot assignments and verified
        # claims, not the order in which classification and inspection arrived.
        for coverage in revised.catalog_coverages:
            for segment in coverage.segments:
                segment.inspect_claim_refs = list(
                    dict.fromkeys(
                        claim_ref
                        for slot_id in segment.slot_ids
                        for claim_ref in by_id[slot_id].inspect_claim_refs
                        if claim_ref in available_claim_refs
                        and (coverage.catalog_id, segment.segment_id)
                        in (claim_segment_ids or {}).get(claim_ref, set())
                    )
                )
        revised.revision += 1
        if update.rationale:
            revised.rationale = update.rationale
        return revised

    def _apply_catalog_review(
        self,
        review: CatalogPageReview,
        *,
        slot_ids: set[str],
    ) -> None:
        if not self.source_coverage_required:
            raise ValueError("catalog reviews require source coverage")
        coverage = next(
            (item for item in self.catalog_coverages if item.catalog_id == review.catalog_id),
            None,
        )
        if coverage is None:
            raise ValueError(f"unknown answer plan catalog: {review.catalog_id}")
        page_segments = [item for item in coverage.segments if item.cursor == review.cursor]
        page_ids = [item.segment_id for item in page_segments]
        decision_ids = [item.segment_id for item in review.decisions]
        if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(page_ids):
            raise ValueError("catalog review decisions must cover the current catalog page exactly")
        unknown_slot_ids = {
            slot_id
            for decision in review.decisions
            for slot_id in decision.slot_ids
            if slot_id not in slot_ids
        }
        if unknown_slot_ids:
            raise ValueError(
                "catalog review references unknown answer slot(s): "
                + ", ".join(sorted(unknown_slot_ids))
            )
        decisions = {item.segment_id: item for item in review.decisions}
        slots_by_id = {slot.id: slot for slot in self.slots}
        for segment in page_segments:
            decision = decisions[segment.segment_id]
            segment.disposition = decision.disposition
            segment.slot_ids = list(dict.fromkeys(decision.slot_ids))
            segment.reason = decision.reason
            if decision.disposition not in {"candidate", "uncertain"}:
                continue
            locator = AnswerEvidenceLocator(
                library_id=coverage.library_id,
                asset_id=coverage.asset_id,
                evidence_id=segment.segment_id,
                segment_id=segment.segment_id,
                page=segment.page,
                time_start=segment.time_start,
                time_end=segment.time_end,
            )
            for slot_id in segment.slot_ids:
                slot = slots_by_id[slot_id]
                slot.candidate_locators = _deduplicate_locators(
                    [*slot.candidate_locators, locator]
                )[:_MAX_CANDIDATE_LOCATORS_PER_SLOT]
                if slot.status == AnswerSlotStatus.PENDING:
                    slot.status = AnswerSlotStatus.CANDIDATE_FOUND

    def observe_search_result(
        self,
        *,
        query: str,
        library_id: str,
        payload: dict[str, Any],
    ) -> RunAnswerPlan:
        """Attach content-free search locators to the slots served by a planned query."""
        query_key = _normalized_query(query)
        slot_ids = {
            str(slot_id)
            for item in self.retrieval_queries
            if isinstance(item, dict)
            and _normalized_query(item.get("query")) == query_key
            and item.get("library_id") == library_id
            for slot_id in item.get("slot_ids") or []
        }
        evidence = payload.get("evidence")
        if not slot_ids or not isinstance(evidence, list):
            return self
        locators = [
            locator
            for item in evidence
            if isinstance(item, dict)
            and (locator := _search_evidence_locator(item, library_id=library_id)) is not None
        ]
        if not locators:
            return self

        revised = self.model_copy(deep=True)
        for slot in revised.slots:
            if slot.id not in slot_ids:
                continue
            slot.candidate_locators = _deduplicate_locators([*slot.candidate_locators, *locators])[
                :_MAX_CANDIDATE_LOCATORS_PER_SLOT
            ]
            if slot.status == AnswerSlotStatus.PENDING:
                slot.status = AnswerSlotStatus.CANDIDATE_FOUND
        revised.revision += 1
        return revised

    def observe_catalog_result(self, payload: dict[str, Any]) -> RunAnswerPlan:
        if payload.get("tool") not in {"list_doc_segments", "list_video_segments"}:
            return self
        mode = str(payload.get("mode") or "")
        if mode not in {"map", "page"}:
            return self
        catalog = payload.get("catalog")
        if not isinstance(catalog, dict):
            return self
        catalog_id = str(catalog.get("catalog_id") or "")
        library_id = str(payload.get("library_id") or "")
        asset_id = str(payload.get("asset_id") or "")
        total_segments = catalog.get("total_segments")
        if not catalog_id or not library_id or not asset_id or not isinstance(total_segments, int):
            return self

        revised = self.model_copy(deep=True)
        coverage = next(
            (item for item in revised.catalog_coverages if item.catalog_id == catalog_id),
            None,
        )
        if coverage is None:
            coverage = AnswerPlanCatalogCoverage(
                catalog_id=catalog_id,
                library_id=library_id,
                asset_id=asset_id,
                total_segments=total_segments,
            )
            revised.catalog_coverages.append(coverage)
        elif (
            coverage.library_id != library_id
            or coverage.asset_id != asset_id
            or coverage.total_segments != total_segments
        ):
            raise ValueError("catalog coverage identity changed within one answer plan")

        if mode == "page":
            raw_cursor = payload.get("cursor")
            start = int(raw_cursor) if raw_cursor not in {None, ""} else 0
            segments = payload.get("segments")
            page_count = len(segments) if isinstance(segments, list) else 0
            end = min(total_segments, start + page_count)
            coverage.scanned_ranges = _merge_scan_ranges(
                [
                    *coverage.scanned_ranges,
                    CatalogScanRange(start_offset=start, end_offset=end),
                ]
            )
            cursor = str(raw_cursor or "")
            incoming_segments = segments if isinstance(segments, list) else []
            incoming_states = [
                _catalog_segment_state(item, cursor=cursor)
                for item in incoming_segments
                if isinstance(item, dict)
            ]
            incoming_ids = [item.segment_id for item in incoming_states]
            if any(not segment_id for segment_id in incoming_ids):
                raise ValueError("catalog page segments require segment_id")
            if len(incoming_ids) != len(set(incoming_ids)):
                raise ValueError("catalog page segment ids must be unique")
            known_by_id = {item.segment_id: item for item in coverage.segments}
            for incoming in incoming_states:
                existing = known_by_id.get(incoming.segment_id)
                if existing is None:
                    coverage.segments.append(incoming)
                    known_by_id[incoming.segment_id] = incoming
                    continue
                if _catalog_segment_locator(existing) != _catalog_segment_locator(incoming):
                    raise ValueError("catalog segment locator changed within one answer plan")
        if revised.catalog_coverages == self.catalog_coverages:
            return self
        revised.revision += 1
        return revised

    def prompt_summary(self) -> dict[str, Any]:
        return {
            "original_question": self.original_question,
            "is_initial_goal": self.is_initial_goal,
            "coverage_requirement": self.coverage_requirement.value,
            "revision": self.revision,
            "rationale": self.rationale,
            "retrieval_queries": self.retrieval_queries,
            "slots": [slot.model_dump(mode="json") for slot in self.slots],
            "unresolved_slot_ids": self.unresolved_slot_ids,
            "catalog_coverage": [
                {
                    "catalog_id": coverage.catalog_id,
                    "asset_id": coverage.asset_id,
                    "total_segments": coverage.total_segments,
                    "scanned_ranges": [
                        item.model_dump(mode="json") for item in coverage.scanned_ranges
                    ],
                    "listing_complete": coverage.listing_complete,
                    "review_complete": coverage.review_complete,
                    "pending_review_pages": coverage.pending_review_pages,
                    "unverified_retained_segment_ids": (coverage.unverified_retained_segment_ids),
                    "disposition_counts": {
                        disposition: sum(
                            segment.disposition == disposition for segment in coverage.segments
                        )
                        for disposition in ("candidate", "uncertain", "excluded")
                    }
                    | {
                        "unreviewed": sum(
                            segment.disposition is None for segment in coverage.segments
                        )
                    },
                }
                for coverage in self.catalog_coverages
            ],
        }


def _normalized_query(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _search_evidence_locator(
    evidence: dict[str, Any],
    *,
    library_id: str,
) -> AnswerEvidenceLocator | None:
    metadata = evidence.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    time_range = evidence.get("time_range") or metadata.get("time_range")
    time_range = time_range if isinstance(time_range, dict) else {}
    segment_ids = evidence.get("segment_ids") or metadata.get("segment_ids") or []
    segment_id = evidence.get("segment_id") or metadata.get("segment_id")
    if not segment_id and isinstance(segment_ids, list) and segment_ids:
        segment_id = segment_ids[0]
    page = evidence.get("page") or metadata.get("page") or metadata.get("page_number")
    locator = AnswerEvidenceLocator(
        library_id=str(evidence.get("library_id") or metadata.get("library_id") or library_id),
        asset_id=str(
            evidence.get("asset_id")
            or metadata.get("asset_id")
            or metadata.get("full_doc_id")
            or ""
        ),
        evidence_id=str(
            evidence.get("evidence_id")
            or evidence.get("chunk_id")
            or evidence.get("id")
            or metadata.get("chunk_id")
            or ""
        ),
        segment_id=str(segment_id or ""),
        page=page if isinstance(page, int) and page >= 1 else None,
        time_start=_non_negative_number(time_range.get("start_seconds")),
        time_end=_non_negative_number(time_range.get("end_seconds")),
    )
    if not any((locator.asset_id, locator.evidence_id, locator.segment_id)):
        return None
    return locator


def _non_negative_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _catalog_segment_state(payload: dict[str, Any], *, cursor: str) -> CatalogSegmentState:
    page_range = payload.get("page_range")
    page_range = page_range if isinstance(page_range, dict) else {}
    time_range = payload.get("time_range")
    time_range = time_range if isinstance(time_range, dict) else {}
    page = page_range.get("start_page")
    return CatalogSegmentState(
        segment_id=str(payload.get("segment_id") or ""),
        cursor=cursor,
        page=page if isinstance(page, int) and page >= 1 else None,
        time_start=_non_negative_number(time_range.get("start_seconds")),
        time_end=_non_negative_number(time_range.get("end_seconds")),
    )


def _catalog_segment_locator(
    segment: CatalogSegmentState,
) -> tuple[int | None, float | None, float | None]:
    return segment.page, segment.time_start, segment.time_end


def _deduplicate_locators(
    locators: list[AnswerEvidenceLocator],
) -> list[AnswerEvidenceLocator]:
    seen: set[tuple[Any, ...]] = set()
    result: list[AnswerEvidenceLocator] = []
    for locator in locators:
        key = (
            locator.library_id,
            locator.asset_id,
            locator.evidence_id,
            locator.segment_id,
            locator.page,
            locator.time_start,
            locator.time_end,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(locator)
    return result


def _merge_scan_ranges(ranges: list[CatalogScanRange]) -> list[CatalogScanRange]:
    merged: list[CatalogScanRange] = []
    for item in sorted(ranges, key=lambda value: (value.start_offset, value.end_offset)):
        if item.end_offset < item.start_offset:
            raise ValueError("catalog scan range end must not precede start")
        if not merged or item.start_offset > merged[-1].end_offset:
            merged.append(item)
            continue
        previous = merged[-1]
        merged[-1] = CatalogScanRange(
            start_offset=previous.start_offset,
            end_offset=max(previous.end_offset, item.end_offset),
        )
    return merged


def _normalized_answer_item_identity(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _merge_verified_items(
    existing: list[VerifiedAnswerItem],
    incoming: list[VerifiedAnswerItem],
) -> list[VerifiedAnswerItem]:
    merged: list[VerifiedAnswerItem] = []
    by_identity: dict[str, VerifiedAnswerItem] = {}
    for item in [*existing, *incoming]:
        identity = _normalized_answer_item_identity(item.identity)
        if not identity:
            raise ValueError("verified answer item identity must contain letters or numbers")
        previous = by_identity.get(identity)
        if previous is None:
            copied = item.model_copy(deep=True)
            copied.inspect_claim_refs = list(dict.fromkeys(copied.inspect_claim_refs))
            by_identity[identity] = copied
            merged.append(copied)
            continue
        if (
            previous.numeric_value is not None
            and item.numeric_value is not None
            and previous.numeric_value != item.numeric_value
        ):
            raise ValueError(f"conflicting numeric values for answer item: {item.identity}")
        if previous.quantity != item.quantity:
            raise ValueError(f"conflicting quantities for answer item: {item.identity}")
        previous.inspect_claim_refs = list(
            dict.fromkeys([*previous.inspect_claim_refs, *item.inspect_claim_refs])
        )
        if previous.numeric_value is None:
            previous.numeric_value = item.numeric_value
    return merged
