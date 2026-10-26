"""Build verified claim bundles and UI citation payloads."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agents.runtime.core.runtime.domain import ToolCallRecord

VIDEO_INSPECT_TOOL_NAMES = frozenset({"video_inspect", "video_inspect_2_0"})

CLAIM_TOOL_NAMES = frozenset(
    {
        *VIDEO_INSPECT_TOOL_NAMES,
        "document_inspect",
        "image_inspect",
        "text_inspect",
        "visual_ground",
        "evidence_asr",
        "evidence_ocr",
    }
)


def collect_claim_bundle(
    completed_tool_calls: Iterable[ToolCallRecord],
    *,
    filter_invalid: bool = False,
) -> list[dict[str, Any]]:
    """Collect claim-generating tool results in call order for citation mapping."""
    from agents.runtime.core.tools.base import unwrap_tool_data

    calls: list[tuple[str, str, dict[str, Any]]] = []
    for call in completed_tool_calls:
        if not isinstance(call, ToolCallRecord):
            continue
        if call.name not in CLAIM_TOOL_NAMES:
            continue
        if not call.result:
            continue
        try:
            data = json.loads(call.result)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            calls.append((call.id, call.name, unwrap_tool_data(data)))

    bundle: list[dict[str, Any]] = []
    for tool_call_id, tool_name, data in calls:
        result = data.get("result", {})
        if not isinstance(result, dict):
            continue
        claims = result.get("claims", [])
        if not isinstance(claims, list):
            continue
        evidence_payload = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        evidence_meta = evidence_payload.get("metadata", {})
        if not isinstance(evidence_meta, dict):
            evidence_meta = {}

        if filter_invalid and tool_name != "text_inspect" and result.get("success") is False:
            continue

        for claim in claims:
            if not isinstance(claim, dict):
                continue
            if filter_invalid:
                if tool_name == "text_inspect":
                    if not claim.get("quote_verified"):
                        continue
                elif claim.get("confidence", 0) < 0.6:
                    continue

            item = _claim_bundle_item(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                claim=claim,
                evidence_payload=evidence_payload,
                evidence_meta=evidence_meta,
            )
            if item:
                bundle.append(item)
    return bundle


def citation_marker_index(marker: str) -> int | None:
    stripped = marker.strip("[]")
    return int(stripped) if stripped.isdigit() else None


def citation_payload(marker: str, bundle_item: dict[str, Any]) -> dict[str, Any]:
    source = _citation_source_payload(bundle_item.get("source"))
    return {
        "marker": marker,
        "claim_id": bundle_item.get("claim_id") or "",
        "citation_index": bundle_item.get("citation_index"),
        "agent_citation": bundle_item.get("agent_citation") or marker,
        "display_label": _citation_display_label(bundle_item, source),
        "tool": bundle_item.get("tool") or "",
        "claim": bundle_item.get("claim") or "",
        "confidence": bundle_item.get("confidence"),
        "quote_verified": bundle_item.get("quote_verified"),
        "verify_reason": bundle_item.get("verify_reason") or "",
        "source": source,
        "render": bundle_item.get("render") or {},
    }


def _claim_bundle_item(
    *,
    tool_name: str,
    tool_call_id: str,
    claim: dict[str, Any],
    evidence_payload: dict[str, Any],
    evidence_meta: dict[str, Any],
) -> dict[str, Any]:
    if tool_name == "text_inspect":
        source = claim.get("source") if isinstance(claim.get("source"), dict) else {}
        source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        segs = source_metadata.get("source_segments") or claim.get("source_segments") or []
        segment_id = str(source.get("segment_id") or (segs[0] if segs else ""))
        return {
            "tool": "text_inspect",
            "tool_call_id": tool_call_id,
            "claim_id": claim.get("claim_id", ""),
            "citation_index": claim.get("citation_index"),
            "agent_citation": claim.get("agent_citation", ""),
            "display_label": claim.get("display_label", ""),
            "source": claim.get("source"),
            "render": claim.get("render"),
            "segment_id": segment_id,
            "claim": claim.get("claim", ""),
            "confidence": claim.get("confidence", 0),
            "quote_verified": claim.get("quote_verified", False),
            "verify_reason": claim.get("verify_reason", ""),
        }
    claim_source = claim.get("source") if isinstance(claim.get("source"), dict) else {}
    if tool_name in {"document_inspect", "image_inspect"} or (
        tool_name in {"visual_ground", "evidence_ocr"} and claim_source.get("type") == "image"
    ):
        source = claim_source
        source_image_id = str(source.get("source_image_id") or claim.get("source_image_id") or "")
        source = _image_inspect_bundle_source(
            source=source,
            claim=claim,
            evidence_payload=evidence_payload,
            evidence_meta=evidence_meta,
            source_image_id=source_image_id,
        )
        return {
            "supports": claim.get("supports") or [],
            "sources": claim.get("sources") or [],
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "claim_id": claim.get("claim_id", ""),
            "citation_index": claim.get("citation_index"),
            "agent_citation": claim.get("agent_citation", ""),
            "display_label": claim.get("display_label", ""),
            "source": source,
            "render": claim.get("render"),
            "segment_id": source_image_id,
            "claim": claim.get("claim", ""),
            "confidence": claim.get("confidence", 0),
            "verify_reason": claim.get("verify_reason", ""),
        }
    if tool_name in (
        "video_inspect",
        "video_inspect_2_0",
        "visual_ground",
        "evidence_asr",
        "evidence_ocr",
    ):
        seg_ids = evidence_meta.get("segment_ids") or []
        seg_id = seg_ids[0] if seg_ids else ""
        ts = claim.get("timestamp_start", 0)
        te = claim.get("timestamp_end", 0)
        return {
            "supports": claim.get("supports") or [],
            "sources": claim.get("sources") or [],
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "claim_id": claim.get("claim_id", ""),
            "citation_index": claim.get("citation_index"),
            "agent_citation": claim.get("agent_citation", ""),
            "display_label": claim.get("display_label", ""),
            "source": claim.get("source"),
            "render": claim.get("render"),
            "segment_id": f"{seg_id}:{int(ts)}-{int(te)}",
            "claim": claim.get("claim", ""),
            "confidence": claim.get("confidence", 0),
            "verify_reason": claim.get("verify_reason", ""),
        }
    return {}


def _image_inspect_bundle_source(
    *,
    source: dict[str, Any],
    claim: dict[str, Any],
    evidence_payload: dict[str, Any],
    evidence_meta: dict[str, Any],
    source_image_id: str,
) -> dict[str, Any]:
    if source.get("type") == "image" and source.get("library_id") and source.get("asset_id"):
        return source
    image_uri = str(source.get("uri") or _evidence_image_uri(evidence_payload) or "")
    library_id = str(source.get("library_id") or evidence_meta.get("library_id") or "")
    asset_id = str(
        source.get("asset_id")
        or evidence_meta.get("asset_id")
        or evidence_meta.get("full_doc_id")
        or ""
    )
    if not any((library_id, asset_id, source_image_id, image_uri)):
        return source
    return {
        **source,
        "type": "image",
        "library_id": library_id,
        "asset_id": asset_id,
        "evidence_id": str(source.get("evidence_id") or evidence_payload.get("id") or ""),
        "title": source.get("title") or evidence_payload.get("title"),
        "segment_id": source.get("segment_id") or evidence_payload.get("id"),
        "source_image_id": source.get("source_image_id") or source_image_id or image_uri,
        "uri": image_uri or source.get("uri") or claim.get("source_image_id"),
    }


def _evidence_image_uri(evidence_payload: dict[str, Any]) -> str:
    parts = evidence_payload.get("parts")
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if not isinstance(part, dict):
            continue
        if str(part.get("kind") or "") == "image":
            return str(part.get("uri") or "")
    return ""


def _citation_display_label(bundle_item: dict[str, Any], source: dict[str, Any]) -> str:
    label = str(bundle_item.get("display_label") or bundle_item.get("segment_id") or "")
    if source.get("type") != "image":
        return label
    if "/" not in label and "\\" not in label:
        return label
    image_name = Path(str(source.get("source_image_id") or source.get("uri") or label)).name
    return f"图片 {image_name}" if image_name else label


def _citation_source_payload(value: Any) -> dict[str, Any]:
    source = dict(value) if isinstance(value, dict) else {}
    if source.get("type") != "image":
        return source
    anchors = source.get("anchors")
    if isinstance(anchors, list) and anchors:
        return source
    image_url = _citation_image_url(source)
    if not image_url:
        return source
    image_name = Path(str(source.get("source_image_id") or source.get("uri") or "")).name
    source["anchors"] = [
        _drop_empty_citation_fields(
            {
                "kind": "image_region",
                "image_uri": source.get("uri") or source.get("source_image_id"),
                "image_url": image_url,
                "source_image_id": image_name,
            }
        )
    ]
    return source


def _citation_image_url(source: dict[str, Any]) -> str | None:
    library_id = str(source.get("library_id") or "").strip()
    asset_id = str(source.get("asset_id") or "").strip()
    image_name = Path(str(source.get("source_image_id") or source.get("uri") or "")).name
    if not library_id or not asset_id or not image_name:
        return None
    return (
        f"/media/libraries/{quote(library_id, safe='')}/assets/"
        f"{quote(asset_id, safe='')}/images/{quote(image_name, safe='')}"
    )


def _drop_empty_citation_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [])}
