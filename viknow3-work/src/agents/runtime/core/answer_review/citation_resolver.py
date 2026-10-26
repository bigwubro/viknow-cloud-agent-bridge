"""Resolve inspect citation markers to real source labels."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARKER_RE = re.compile(r"\[(\d+|H\d+|claim_[A-Za-z0-9_-]+|claim:[^\]\s]+)\]")


@dataclass
class ResolvedCitation:
    marker: str
    index: int
    segment_id: str


@dataclass
class ResolveResult:
    resolved_text: str
    citations: list[ResolvedCitation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def resolve_citations(text: str, inspect_bundle: list[dict]) -> ResolveResult:
    """Resolve citation markers to real source labels.

    Numeric markers keep the legacy ``[N]`` mapping where
    ``inspect_bundle[i - 1]`` maps to ``[N]``. Claim markers resolve by the
    structured ``claim_id`` now returned by inspect tools.
    """
    errors: list[str] = []
    matches: list[tuple[str, str]] = []

    for m in _MARKER_RE.finditer(text):
        matches.append((m.group(0), m.group(1)))

    if not matches:
        errors.append("missing_citation: no citation markers found in answer")

    resolved = text
    citations: list[ResolvedCitation] = []
    has_citation_indexes = any(item.get("citation_index") is not None for item in inspect_bundle)
    claims_by_index = {
        int(item.get("citation_index")): (index, item)
        for index, item in enumerate(inspect_bundle, start=1)
        if _numeric_agent_citation_matches_index(item)
    }
    claims_by_id = {
        str(item.get("claim_id")): (index, item)
        for index, item in enumerate(inspect_bundle, start=1)
        if item.get("claim_id")
    }
    claims_by_marker = {
        str(item.get("agent_citation")): (index, item)
        for index, item in enumerate(inspect_bundle, start=1)
        if item.get("agent_citation")
    }
    claims_by_ref = {
        str(item.get("claim_ref")): (index, item)
        for index, item in enumerate(inspect_bundle, start=1)
        if item.get("claim_ref")
    }

    for marker, token in matches:
        exact_match = claims_by_marker.get(marker)
        ref_match = claims_by_ref.get(token)
        if exact_match is not None:
            idx, bundle_item = exact_match
        elif ref_match is not None:
            idx, bundle_item = ref_match
        elif token.isdigit():
            idx = int(token)
            if has_citation_indexes:
                match = claims_by_index.get(idx)
                if match is None:
                    errors.append(f"invalid_citation: [{idx}] not found in inspect claims")
                    continue
                idx, bundle_item = match
            elif idx < 1 or idx > len(inspect_bundle):
                errors.append(
                    f"invalid_citation: [{idx}] out of range "
                    f"(bundle has {len(inspect_bundle)} items)"
                )
                continue
            else:
                bundle_item = inspect_bundle[idx - 1]
        else:
            match = claims_by_id.get(token)
            if match is None:
                errors.append(f"invalid_citation: [{token}] not found in inspect claims")
                continue
            idx, bundle_item = match

        segment_id = _resolved_label(bundle_item)
        if not segment_id:
            errors.append(f"invalid_citation: {marker} has empty source label")
            continue
        resolved = resolved.replace(marker, f"[{segment_id}]")
        citations.append(ResolvedCitation(marker=marker, index=idx, segment_id=segment_id))

    return ResolveResult(resolved_text=resolved, citations=citations, errors=errors)


def _resolved_label(bundle_item: dict) -> str:
    display_label = bundle_item.get("display_label")
    if isinstance(display_label, str) and display_label.strip():
        return display_label.strip()
    segment_id = bundle_item.get("segment_id")
    if isinstance(segment_id, str) and segment_id.strip():
        return segment_id.strip()
    source = bundle_item.get("source")
    if isinstance(source, dict):
        for key in ("segment_id", "source_image_id", "evidence_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _numeric_agent_citation_matches_index(bundle_item: dict) -> bool:
    citation_index = bundle_item.get("citation_index")
    if not isinstance(citation_index, int):
        return False
    return bundle_item.get("agent_citation") == f"[{citation_index}]"
