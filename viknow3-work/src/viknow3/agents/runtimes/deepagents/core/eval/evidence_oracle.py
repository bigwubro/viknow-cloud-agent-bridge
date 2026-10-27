"""Read-only, scope-bound evidence packages for offline skill-flow evaluation."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from viknow3.agents.runtimes.deepagents.core.eval.skill_flow import SkillEvalCase

EvidenceDisposition = Literal["supports", "contradicts", "unknown"]


class EvidenceOracleScope(BaseModel):
    """The only knowledge range an oracle source may inspect."""

    model_config = ConfigDict(extra="forbid")

    library_id: str
    asset_ids: list[str] = Field(default_factory=list)
    document_set_id: str | None = None
    asset_revisions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_asset_revisions(self) -> EvidenceOracleScope:
        if not set(self.asset_revisions).issubset(self.asset_ids):
            raise ValueError("asset_revisions keys must be included in asset_ids hints")
        if any(not revision.strip() for revision in self.asset_revisions.values()):
            raise ValueError("asset revisions must not be empty")
        return self


class EvidenceOracleFact(BaseModel):
    """One oracle conclusion, optionally carrying its accepted fact statement."""

    model_config = ConfigDict(extra="forbid")

    gold_id: str
    disposition: EvidenceDisposition
    locator: dict[str, Any] = Field(default_factory=dict)
    source_ref: str
    statement: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceOracleRequirement(BaseModel):
    """Evidence disposition for one required fact; alternatives are grouped together."""

    requirement_id: str
    disposition: EvidenceDisposition
    gold_ids: list[str]
    accepted_facts: list[AcceptedOracleFact] = Field(default_factory=list)


class AcceptedOracleFact(BaseModel):
    """One human-accepted, modality-independent fact used for offline scoring."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    statement: str
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceOraclePackage(BaseModel):
    """Versioned package that a scorer can consume without re-reading raw sources."""

    schema_version: Literal[1] = 1
    case_id: str
    scope: EvidenceOracleScope
    asset_revisions: dict[str, str]
    facts: list[EvidenceOracleFact] = Field(default_factory=list)
    requirements: list[EvidenceOracleRequirement] = Field(default_factory=list)
    gap_requirement_ids: list[str] = Field(default_factory=list)

    @property
    def storage_key(self) -> str:
        document_set_id = self.scope.document_set_id or "-"
        assets = ",".join(sorted(self.scope.asset_ids))
        revisions = ",".join(
            f"{asset_id}@{revision}" for asset_id, revision in sorted(self.asset_revisions.items())
        )
        return f"{self.case_id}:{self.scope.library_id}:{document_set_id}:{revisions}:{assets}"

    def is_reusable_for(self, *, case_id: str, scope: EvidenceOracleScope) -> bool:
        return (
            self.case_id == case_id
            and self.scope == scope
            and self.asset_revisions == scope.asset_revisions
        )


class EvidenceOracleSource(Protocol):
    """A read-only adapter; implementations must honor the supplied scope."""

    def resolve(
        self,
        *,
        scope: EvidenceOracleScope,
        locators: list[dict[str, object]],
    ) -> list[EvidenceOracleFact]: ...


class AssetSegmentReader(Protocol):
    """Minimal read-only boundary already implemented by the knowledge adapter."""

    def list_asset_segments(self, *, library_id: str, asset_id: str) -> list[dict[str, Any]]: ...


class AssetSegmentCoverageSource:
    """Report indexed locator reachability without making a semantic claim.

    A segment overlap only proves that the configured index exposes a location.
    It intentionally yields ``unknown`` rather than ``supports``; semantic
    evidence must come from an inspect or curated source.
    """

    def __init__(self, reader: AssetSegmentReader) -> None:
        self._reader = reader

    def resolve(
        self,
        *,
        scope: EvidenceOracleScope,
        locators: list[dict[str, object]],
    ) -> list[EvidenceOracleFact]:
        segments_by_asset: dict[str, list[dict[str, Any]]] = {}
        facts: list[EvidenceOracleFact] = []
        for locator in locators:
            gold_id = str(locator.get("gold_id") or "")
            asset_id = str(locator.get("asset_id") or "")
            if not gold_id or asset_id not in scope.asset_ids:
                continue
            if asset_id not in segments_by_asset:
                segments_by_asset[asset_id] = self._reader.list_asset_segments(
                    library_id=scope.library_id,
                    asset_id=asset_id,
                )
            segments = segments_by_asset[asset_id]
            matched = _segment_covering(segments, locator.get("time_range"))
            if matched is None:
                continue
            segment_id = str(matched.get("id") or matched.get("segment_id") or "unknown")
            facts.append(
                EvidenceOracleFact(
                    gold_id=gold_id,
                    disposition="unknown",
                    locator={
                        "library_id": scope.library_id,
                        "asset_id": asset_id,
                        **(
                            {"time_range": locator["time_range"]}
                            if isinstance(locator.get("time_range"), dict)
                            else {}
                        ),
                    },
                    source_ref=f"segment://{asset_id}/{segment_id}",
                )
            )
        return facts


class CuratedGoldEvidenceSource:
    """Use explicitly curated fact statements as a semantic ground-truth source.

    This is restricted to offline eval fixtures. The statements are supplied
    by a dataset adapter after human review; this source does not infer facts
    from index fields or from a particular modality's caption field.
    """

    def __init__(self, statements_by_gold_id: dict[str, str]) -> None:
        self._statements_by_gold_id = {
            gold_id: statement.strip()
            for gold_id, statement in statements_by_gold_id.items()
            if statement.strip()
        }

    def resolve(
        self,
        *,
        scope: EvidenceOracleScope,
        locators: list[dict[str, object]],
    ) -> list[EvidenceOracleFact]:
        facts: list[EvidenceOracleFact] = []
        for locator in locators:
            gold_id = str(locator.get("gold_id") or "")
            asset_id = str(locator.get("asset_id") or "")
            statement = self._statements_by_gold_id.get(gold_id, "")
            if not gold_id or not statement or asset_id not in scope.asset_ids:
                continue
            fact_locator: dict[str, Any] = {
                "library_id": scope.library_id,
                "asset_id": asset_id,
            }
            if isinstance(locator.get("time_range"), dict):
                fact_locator["time_range"] = locator["time_range"]
            facts.append(
                EvidenceOracleFact(
                    gold_id=gold_id,
                    disposition="supports",
                    locator=fact_locator,
                    source_ref=f"curated-gold://{gold_id}",
                    statement=statement,
                    confidence=1.0,
                )
            )
        return facts


class EvidenceOracle:
    """Build deterministic fact packages and reject evidence outside the authorized scope."""

    def __init__(self, source: EvidenceOracleSource) -> None:
        self._source = source

    def build(self, *, case: SkillEvalCase, scope: EvidenceOracleScope) -> EvidenceOraclePackage:
        gold_by_id = {
            str(gold.get("gold_id") or index): gold
            for index, gold in enumerate(case.gold_citations, start=1)
        }
        self._validate_gold_scope(gold_by_id.values(), scope)
        facts = self._source.resolve(
            scope=scope,
            locators=[dict(gold) for gold in gold_by_id.values()],
        )
        for fact in facts:
            if fact.gold_id not in gold_by_id:
                raise ValueError(f"oracle fact refers to unknown gold id: {fact.gold_id}")
            self._validate_fact_scope(fact, scope)

        requirements = _requirements(gold_by_id, facts)
        return EvidenceOraclePackage(
            case_id=case.case_id,
            scope=scope,
            asset_revisions=scope.asset_revisions,
            facts=facts,
            requirements=requirements,
            gap_requirement_ids=[
                item.requirement_id for item in requirements if item.disposition == "unknown"
            ],
        )

    @staticmethod
    def _validate_gold_scope(
        gold_citations: Any,
        scope: EvidenceOracleScope,
    ) -> None:
        for gold in gold_citations:
            if not isinstance(gold, dict):
                continue
            library_id = str(gold.get("workspace") or gold.get("library_id") or "")
            asset_id = str(gold.get("asset_id") or "")
            if library_id and library_id != scope.library_id:
                raise ValueError("gold locator outside authorized scope")
            if asset_id and asset_id not in scope.asset_ids:
                raise ValueError("gold locator outside authorized scope")

    @staticmethod
    def _validate_fact_scope(fact: EvidenceOracleFact, scope: EvidenceOracleScope) -> None:
        library_id = str(fact.locator.get("library_id") or fact.locator.get("workspace") or "")
        asset_id = str(fact.locator.get("asset_id") or fact.locator.get("document_id") or "")
        if library_id != scope.library_id or asset_id not in scope.asset_ids:
            raise ValueError("oracle fact outside authorized scope")


def _requirements(
    gold_by_id: dict[str, dict[str, Any]],
    facts: list[EvidenceOracleFact],
) -> list[EvidenceOracleRequirement]:
    facts_by_gold: dict[str, list[EvidenceOracleFact]] = {}
    for fact in facts:
        facts_by_gold.setdefault(fact.gold_id, []).append(fact)

    grouped: dict[str, list[str]] = {}
    for gold_id, gold in gold_by_id.items():
        requirement_id = str(gold.get("requirement_id") or gold_id)
        grouped.setdefault(requirement_id, []).append(gold_id)

    requirements: list[EvidenceOracleRequirement] = []
    for requirement_id, gold_ids in grouped.items():
        dispositions = {
            fact.disposition for gold_id in gold_ids for fact in facts_by_gold.get(gold_id, [])
        }
        disposition: EvidenceDisposition
        if "supports" in dispositions:
            disposition = "supports"
        elif "contradicts" in dispositions:
            disposition = "contradicts"
        else:
            disposition = "unknown"
        accepted_facts = [
            AcceptedOracleFact(
                fact_id=gold_id,
                statement=supporting_fact.statement,
                evidence_refs=[supporting_fact.locator],
                source_refs=[supporting_fact.source_ref],
                confidence=supporting_fact.confidence,
            )
            for gold_id in gold_ids
            for supporting_fact in facts_by_gold.get(gold_id, [])
            if supporting_fact.disposition == "supports" and supporting_fact.statement
        ]
        requirements.append(
            EvidenceOracleRequirement(
                requirement_id=requirement_id,
                disposition=disposition,
                gold_ids=gold_ids,
                accepted_facts=accepted_facts,
            )
        )
    return requirements


def _segment_covering(
    segments: list[dict[str, Any]],
    target_range: object,
) -> dict[str, Any] | None:
    target = _time_bounds(target_range)
    if target is None:
        return segments[0] if segments else None
    target_start, target_end = target
    for segment in segments:
        segment_range = segment.get("time_range")
        if not isinstance(segment_range, dict):
            continue
        bounds = _time_bounds(segment_range)
        if bounds is None:
            continue
        start, end = bounds
        if start <= target_start and target_end <= end:
            return segment
    return None


def _time_bounds(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    start = value.get("start_seconds")
    end = value.get("end_seconds")
    if not isinstance(start, int | float) or not isinstance(end, int | float):
        return None
    if end < start:
        return None
    return float(start), float(end)
