"""Deterministic next-action selection for offline Evidence Oracle research."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.research_oracle import (
    CoverageEntry,
    EvidenceFrontierItem,
    ResearchPlan,
)

ResearchActionKind = Literal["select", "page", "inspect", "complete", "incomplete"]

_SOURCE_PRIORITY = {"qa_citation": 0, "discovery": 1, "catalog": 2, "adjacent": 3}


class CatalogEntry(BaseModel):
    """One compact catalog entry used only for deterministic sharding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    locator_id: str = Field(min_length=1)
    estimated_tokens: int = Field(ge=1)


class CatalogManifest(BaseModel):
    """Ordered compact catalog metadata; never carries raw segment content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(min_length=1)
    entries: list[CatalogEntry] = Field(min_length=1)


class ResearchShard(BaseModel):
    """One sequential, bounded catalog slice delegated to a Scout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    entries: list[CatalogEntry] = Field(min_length=1)


def build_sequential_shards(
    manifest: CatalogManifest,
    *,
    max_estimated_tokens: int,
) -> list[ResearchShard]:
    """Partition an ordered catalog without splitting an individual entry."""

    if max_estimated_tokens < 1:
        raise ValueError("max_estimated_tokens must be positive")

    shards: list[ResearchShard] = []
    current: list[CatalogEntry] = []
    current_tokens = 0
    for entry in manifest.entries:
        if current and current_tokens + entry.estimated_tokens > max_estimated_tokens:
            shards.append(
                ResearchShard(
                    catalog_id=manifest.catalog_id,
                    ordinal=len(shards),
                    entries=current,
                )
            )
            current = []
            current_tokens = 0
        current.append(entry)
        current_tokens += entry.estimated_tokens
    shards.append(
        ResearchShard(
            catalog_id=manifest.catalog_id,
            ordinal=len(shards),
            entries=current,
        )
    )
    return shards


class ResearchBudget(BaseModel):
    """Fixed, offline-enforced limits for one scoped research task."""

    model_config = ConfigDict(extra="forbid")

    max_frontier_items: int = Field(ge=1)
    max_inspect_actions: int = Field(ge=1)
    max_research_rounds: int = Field(ge=1)
    max_video_inspect_seconds: int = Field(ge=1, le=30)


class ResearchCatalogProgress(BaseModel):
    """A catalog page made available by a prior bounded map/page result."""

    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=1)
    next_cursor: str | None = None


class ResearchHistory(BaseModel):
    """Already-consumed actions; this is supplied by replay or a future runtime adapter."""

    model_config = ConfigDict(extra="forbid")

    inspected_frontier_ids: list[str] = Field(default_factory=list)
    selected_requirement_ids: list[str] = Field(default_factory=list)
    paged_catalog_cursors: list[str] = Field(default_factory=list)
    catalog_progress: list[ResearchCatalogProgress] = Field(default_factory=list)
    inspect_actions_used: int = Field(default=0, ge=0)
    research_rounds_used: int = Field(default=0, ge=0)


class ResearchAction(BaseModel):
    """One actionable, bounded next step, or an explicit terminal state."""

    model_config = ConfigDict(extra="forbid")

    kind: ResearchActionKind
    requirement_ids: list[str] = Field(default_factory=list)
    frontier_ids: list[str] = Field(default_factory=list)
    query: str | None = None
    catalog_id: str | None = None
    cursor: str | None = None
    reason_code: str
    reason: str


class ResearchController:
    """Choose a next research action without performing a tool call or inference."""

    def next_action(
        self,
        *,
        plan: ResearchPlan,
        frontier: list[EvidenceFrontierItem],
        coverage: list[CoverageEntry],
        budget: ResearchBudget,
        history: ResearchHistory,
    ) -> ResearchAction:
        coverage_by_id = {entry.requirement_id: entry for entry in coverage}
        incomplete_ids = [
            subquestion.id
            for subquestion in plan.subquestions
            if not coverage_by_id.get(subquestion.id, _missing_coverage(subquestion.id)).is_complete
        ]
        if not incomplete_ids:
            return ResearchAction(
                kind="complete",
                reason_code="coverage_complete",
                reason="All planned coverage checks are complete.",
            )
        if history.inspect_actions_used >= budget.max_inspect_actions:
            return _incomplete("inspect_budget_exhausted", "Inspect action budget is exhausted.")
        if history.research_rounds_used >= budget.max_research_rounds:
            return _incomplete(
                "research_round_budget_exhausted",
                "Research round budget is exhausted.",
            )

        inspectable = _deduplicated_frontier(
            frontier,
            incomplete_ids=incomplete_ids,
            inspected_ids=set(history.inspected_frontier_ids),
        )
        if inspectable:
            requirement_id = next(
                requirement_id
                for requirement_id in incomplete_ids
                if any(requirement_id in item.requirement_ids for item in inspectable)
            )
            selected = [item for item in inspectable if requirement_id in item.requirement_ids][
                : budget.max_frontier_items
            ]
            deduplicated = len(inspectable) < len(
                [
                    item
                    for item in frontier
                    if requirement_id in item.requirement_ids
                    and item.frontier_id not in history.inspected_frontier_ids
                ]
            )
            return ResearchAction(
                kind="inspect",
                requirement_ids=[requirement_id],
                frontier_ids=[item.frontier_id for item in selected],
                reason_code="uncovered_frontier",
                reason=(
                    "Inspect the highest-priority candidates for the earliest uncovered "
                    "requirement" + (" after deduplicated locators." if deduplicated else ".")
                ),
            )

        consumed_pages = set(history.paged_catalog_cursors)
        for progress in history.catalog_progress:
            if progress.next_cursor is None:
                continue
            page_key = f"{progress.catalog_id}:{progress.next_cursor}"
            if page_key not in consumed_pages:
                return ResearchAction(
                    kind="page",
                    requirement_ids=incomplete_ids,
                    catalog_id=progress.catalog_id,
                    cursor=progress.next_cursor,
                    reason_code="coverage_gap_requires_page",
                    reason=(
                        "Review the next bounded catalog page before lexical selection so "
                        "the Scout can assess caption, ASR, and temporal coverage."
                    ),
                )

        selected_ids = set(history.selected_requirement_ids)
        for subquestion in plan.subquestions:
            if subquestion.id in incomplete_ids and subquestion.id not in selected_ids:
                return ResearchAction(
                    kind="select",
                    requirement_ids=[subquestion.id],
                    query=subquestion.question,
                    reason_code="coverage_gap_requires_selection",
                    reason=(
                        "No inspectable locator remains; select candidates for the uncovered "
                        "requirement."
                    ),
                )

        return _incomplete(
            "coverage_gap_no_remaining_action",
            "Coverage remains incomplete and no unconsumed frontier, selection, or page exists.",
        )


def _missing_coverage(requirement_id: str) -> CoverageEntry:
    return CoverageEntry(requirement_id=requirement_id, required_checks=["unrecorded"])


def _deduplicated_frontier(
    frontier: list[EvidenceFrontierItem],
    *,
    incomplete_ids: list[str],
    inspected_ids: set[str],
) -> list[EvidenceFrontierItem]:
    relevant = [
        item
        for item in frontier
        if item.frontier_id not in inspected_ids
        and any(requirement_id in incomplete_ids for requirement_id in item.requirement_ids)
    ]
    best_by_locator: dict[tuple[str, str, str], EvidenceFrontierItem] = {}
    for item in relevant:
        identity = _locator_identity(item)
        current = best_by_locator.get(identity)
        if current is None or _frontier_sort_key(item, incomplete_ids) < _frontier_sort_key(
            current, incomplete_ids
        ):
            best_by_locator[identity] = item
    return sorted(
        best_by_locator.values(),
        key=lambda item: _frontier_sort_key(item, incomplete_ids),
    )


def _locator_identity(item: EvidenceFrontierItem) -> tuple[str, str, str]:
    locator = item.locator
    return (
        str(locator.get("library_id") or locator.get("workspace") or ""),
        str(locator.get("asset_id") or locator.get("document_id") or ""),
        str(locator.get("locator_id") or locator.get("segment_id") or ""),
    )


def _frontier_sort_key(
    item: EvidenceFrontierItem,
    incomplete_ids: list[str],
) -> tuple[int, int, int, str]:
    requirement_priority = min(
        incomplete_ids.index(requirement_id)
        for requirement_id in item.requirement_ids
        if requirement_id in incomplete_ids
    )
    return (
        requirement_priority,
        _SOURCE_PRIORITY[item.source],
        item.research_round,
        item.frontier_id,
    )


def _incomplete(reason_code: str, reason: str) -> ResearchAction:
    return ResearchAction(kind="incomplete", reason_code=reason_code, reason=reason)
