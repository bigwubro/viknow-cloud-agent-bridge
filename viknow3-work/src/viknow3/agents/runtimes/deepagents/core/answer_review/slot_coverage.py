"""Evidence-linked slot observations; never infer coverage from answer fluency."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AbsoluteRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds < self.start_seconds:
            raise ValueError("absolute range must be ordered")
        return self


class EventInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    claim_ref: str
    role: str = ""
    entity_id: str = ""
    identity_basis: list[Literal["face", "voice", "continuity", "clothing", "other"]] = Field(
        default_factory=list
    )


class SlotAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: str
    required_modalities: list[Literal["text", "video", "image", "asr", "ocr"]] = Field(
        default_factory=list
    )
    claim_refs: list[str] = Field(default_factory=list)
    event_instances: list[EventInstance] = Field(default_factory=list)
    required_event_ids: list[str] = Field(default_factory=list)
    required_roles: list[str] = Field(default_factory=list)
    required_ranges: list[AbsoluteRange] = Field(default_factory=list)
    requires_cross_scene_identity: bool = False
    conflicting_claim_refs: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)


def interval_gaps(required: AbsoluteRange, windows: list[AbsoluteRange]) -> list[AbsoluteRange]:
    cursor = required.start_seconds
    gaps = []
    for window in sorted(windows, key=lambda item: item.start_seconds):
        if window.asset_id != required.asset_id or window.end_seconds < cursor:
            continue
        if window.start_seconds > required.end_seconds:
            break
        if window.start_seconds > cursor:
            gaps.append(
                AbsoluteRange(
                    asset_id=required.asset_id,
                    start_seconds=cursor,
                    end_seconds=min(window.start_seconds, required.end_seconds),
                )
            )
        cursor = max(cursor, min(window.end_seconds, required.end_seconds))
    if cursor < required.end_seconds:
        gaps.append(
            AbsoluteRange(
                asset_id=required.asset_id, start_seconds=cursor, end_seconds=required.end_seconds
            )
        )
    return gaps


def resolve_slot_assessment(assessment: SlotAssessment, slot: dict, facts: list[dict]) -> dict:
    """Validate references against this run and derive time from actual claim sources."""
    eligible = {fact["claim_ref"]: fact for fact in facts if fact.get("status") == "verified"}
    requested_refs = set(assessment.claim_refs) | {
        event.claim_ref for event in assessment.event_instances
    }
    selected = [eligible[ref] for ref in requested_refs if ref in eligible]
    missing = list(assessment.missing_conditions)
    if requested_refs - eligible.keys():
        missing.append("unverified_claim_reference")
    if not selected:
        missing.append("missing_verified_evidence")
    modalities = {modality for fact in selected for modality in fact.get("modalities", [])}
    for modality in set(slot.get("required_modalities") or []) | set(
        assessment.required_modalities
    ):
        if modality not in modalities:
            missing.append("missing_modality:" + modality)
    instances = []
    for event in assessment.event_instances:
        fact = eligible.get(event.claim_ref)
        if fact is None:
            continue
        locator = fact.get("locator") or {}
        interval = locator.get("time_range")
        if interval is None and (
            assessment.required_roles or assessment.required_event_ids or assessment.required_ranges
        ):
            missing.append("missing_absolute_event_time")
        instances.append(
            {**event.model_dump(mode="json"), "source": locator, "absolute_time_range": interval}
        )
        if assessment.requires_cross_scene_identity and not (
            set(event.identity_basis) & {"face", "voice", "continuity"}
        ):
            missing.append("cross_scene_identity_unverified")
    if len({item["claim_ref"] for item in instances}) < len(
        {item["event_id"] for item in instances}
    ):
        missing.append("events_without_distinct_evidence")
    event_ids = {item["event_id"] for item in instances}
    roles = {item["role"] for item in instances}
    missing.extend(
        "missing_event:" + key for key in assessment.required_event_ids if key not in event_ids
    )
    missing.extend(
        "missing_endpoint:" + key for key in assessment.required_roles if key not in roles
    )
    if {"before", "after"} <= set(assessment.required_roles):
        before = [
            eligible[event.claim_ref]
            for event in assessment.event_instances
            if event.role == "before" and event.claim_ref in eligible
        ]
        after = [
            eligible[event.claim_ref]
            for event in assessment.event_instances
            if event.role == "after" and event.claim_ref in eligible
        ]
        for left in before:
            for right in after:
                a, b = left.get("locator") or {}, right.get("locator") or {}
                if a.get("asset_id") != b.get("asset_id"):
                    missing.append("comparison_source_unresolved")
                if not a.get("time_range") or not b.get("time_range"):
                    missing.append("missing_absolute_event_time")
                elif a["time_range"] == b["time_range"]:
                    missing.append("comparison_endpoints_not_distinct")
                elif a["time_range"]["end_seconds"] > b["time_range"]["start_seconds"]:
                    missing.append("comparison_temporal_order_unresolved")
                same_window = left.get("sampling_range") == right.get("sampling_range")
                left_frames = {
                    frame
                    for support in left.get("supports", [])
                    for frame in support.get("frame_ids", [])
                }
                right_frames = {
                    frame
                    for support in right.get("supports", [])
                    for frame in support.get("frame_ids", [])
                }
                if same_window and left_frames & right_frames:
                    missing.append("comparison_endpoints_not_distinct")
    windows = []
    keys = set()
    for fact in selected:
        locator = fact.get("locator") or {}
        interval = locator.get("time_range")
        if interval and locator.get("asset_id"):
            window = AbsoluteRange(asset_id=locator["asset_id"], **interval)
            windows.append(window)
            sampled = fact.get("sampling_range") or interval
            keys.add((window.asset_id, sampled["start_seconds"], sampled["end_seconds"]))
    gaps = [
        gap for required in assessment.required_ranges for gap in interval_gaps(required, windows)
    ]
    if gaps:
        missing.append("missing_temporal_range")
    conflicts = [ref for ref in assessment.conflicting_claim_refs if ref in eligible]
    if conflicts:
        missing.append("unresolved_claim_conflict")
    return {
        "slot_id": assessment.slot_id,
        "claim_refs": sorted(requested_refs & eligible.keys()),
        "event_instances": instances,
        "verified_ranges": [w.model_dump() for w in windows],
        "missing_ranges": [gap.model_dump() for gap in gaps],
        "independent_evidence_count": len(keys),
        "conflicting_claim_refs": conflicts,
        "unresolved_gaps": list(dict.fromkeys(missing)),
        "status": "unknown" if missing else "covered",
    }
