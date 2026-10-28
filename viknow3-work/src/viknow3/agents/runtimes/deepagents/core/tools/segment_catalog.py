"""Stable, bounded views over an asset's indexed segments.

The catalog is deliberately content-addressed by the caller and contains no
agent-specific policy.  Tool handlers use it to expose map, page, select, and
coverage operations without returning an entire asset in one tool result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

AssetKind = Literal["video", "document"]


@dataclass(frozen=True)
class SegmentRecord:
    segment_id: str
    order: int
    summary: str
    content: str = ""
    metadata: dict[str, Any] | None = None
    time_range: dict[str, float] | None = None
    page_range: dict[str, int] | None = None
    section: str = ""
    candidate_evidence: tuple[dict[str, Any], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "segment_id": self.segment_id,
            "order": self.order,
            "summary": self.summary,
        }
        if self.time_range is not None:
            payload["time_range"] = self.time_range
        if self.page_range is not None:
            payload["page_range"] = self.page_range
        if self.section:
            payload["section"] = self.section
        if self.candidate_evidence:
            payload["candidate_evidence"] = [dict(item) for item in self.candidate_evidence]
        return payload


@dataclass(frozen=True)
class CatalogPage:
    records: tuple[SegmentRecord, ...]
    next_cursor: str | None
    total_records: int


@dataclass(frozen=True)
class CatalogSelection:
    matches: tuple[SegmentRecord, ...]
    neighbors: tuple[SegmentRecord, ...]
    selection_basis: str


@dataclass(frozen=True)
class CatalogCoverage:
    total_records: int
    verified_segment_ids: tuple[str, ...]
    attempted_segment_ids: tuple[str, ...]
    uninspected_segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class SegmentCatalog:
    catalog_id: str
    library_id: str
    asset_id: str
    asset_kind: AssetKind
    index_revision: str
    records: tuple[SegmentRecord, ...]

    @classmethod
    def create(
        cls,
        *,
        catalog_id: str,
        library_id: str,
        asset_id: str,
        asset_kind: AssetKind,
        index_revision: str,
        records: list[SegmentRecord],
    ) -> SegmentCatalog:
        if not catalog_id or not library_id or not asset_id or not index_revision:
            raise ValueError("catalog identity, scope, and index_revision are required")
        ordered = tuple(sorted(records, key=lambda item: (item.order, item.segment_id)))
        if len({item.segment_id for item in ordered}) != len(ordered):
            raise ValueError("segment_id must be unique within a catalog")
        return cls(
            catalog_id=catalog_id,
            library_id=library_id,
            asset_id=asset_id,
            asset_kind=asset_kind,
            index_revision=index_revision,
            records=ordered,
        )

    def page(self, *, cursor: str | None, page_size: int) -> CatalogPage:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        offset = _parse_cursor(cursor, total=len(self.records))
        page = self.records[offset : offset + page_size]
        next_offset = offset + len(page)
        return CatalogPage(
            records=page,
            next_cursor=str(next_offset) if next_offset < len(self.records) else None,
            total_records=len(self.records),
        )

    def select(
        self,
        *,
        query: str,
        limit: int,
        neighbor_count: int,
    ) -> CatalogSelection:
        if limit < 1:
            raise ValueError("limit must be positive")
        if neighbor_count < 0:
            raise ValueError("neighbor_count must not be negative")
        terms = _query_terms(query)
        if not terms:
            raise ValueError("query must contain at least one searchable term")
        scored = [(index, _match_score(record, terms)) for index, record in enumerate(self.records)]
        selected_indexes = [
            index
            for index, score in sorted(scored, key=lambda item: (-item[1], item[0]))
            if score > 0
        ][:limit]
        selected = tuple(self.records[index] for index in selected_indexes)
        neighbor_indexes: set[int] = set()
        for index in selected_indexes:
            neighbor_indexes.update(
                range(
                    max(0, index - neighbor_count),
                    min(len(self.records), index + neighbor_count + 1),
                )
            )
        neighbor_indexes.difference_update(selected_indexes)
        return CatalogSelection(
            matches=selected,
            neighbors=tuple(self.records[index] for index in sorted(neighbor_indexes)),
            selection_basis="query_terms" if selected else "query_terms_no_match",
        )

    def coverage(
        self,
        *,
        verified_segment_ids: set[str],
        attempted_segment_ids: set[str],
    ) -> CatalogCoverage:
        known_ids = {record.segment_id for record in self.records}
        verified = tuple(
            record.segment_id
            for record in self.records
            if record.segment_id in verified_segment_ids
        )
        attempted = tuple(
            record.segment_id
            for record in self.records
            if record.segment_id in attempted_segment_ids
            and record.segment_id not in verified_segment_ids
        )
        uninspected = tuple(
            record.segment_id
            for record in self.records
            if record.segment_id not in verified_segment_ids
            and record.segment_id not in attempted_segment_ids
        )
        unknown = (verified_segment_ids | attempted_segment_ids) - known_ids
        if unknown:
            raise ValueError("coverage references a segment outside this catalog")
        return CatalogCoverage(
            total_records=len(self.records),
            verified_segment_ids=verified,
            attempted_segment_ids=attempted,
            uninspected_segment_ids=uninspected,
        )


def _parse_cursor(cursor: str | None, *, total: int) -> int:
    if cursor in {None, ""}:
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative integer offset") from exc
    if offset < 0 or offset > total:
        raise ValueError("cursor is outside this catalog")
    return offset


def _query_terms(query: str) -> tuple[str, ...]:
    normalized = query.strip().casefold()
    tokens = re.findall(r"[^\W\d_]+|[a-z0-9_]+", normalized)
    return tuple(dict.fromkeys(token for token in tokens if len(token) >= 2))


def _match_score(record: SegmentRecord, terms: tuple[str, ...]) -> int:
    metadata = record.metadata or {}
    metadata_text = " ".join(str(value) for value in metadata.values() if isinstance(value, str))
    candidate_text = " ".join(
        str(candidate.get("text") or "") for candidate in record.candidate_evidence
    )
    haystack = " ".join(
        (record.summary, record.content, record.section, metadata_text, candidate_text)
    ).casefold()
    return sum(term in haystack for term in terms)
