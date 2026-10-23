"""Validation for extracted video KB specs."""

from __future__ import annotations

from typing import Any

from viknow.core.knowledge.video_indexing.domain import ValidationIssue, ValidationReport


class SpecValidator:
    """Validate draft specs before index building or publication."""

    def validate_spec(self, spec: dict[str, Any]) -> ValidationReport:
        issues: list[ValidationIssue] = []
        segments = spec.get("segments", [])
        sop_ids = {item.get("id") for item in spec.get("sop_steps", [])}
        item_state_ids = {item.get("id") for item in spec.get("item_states", [])}
        object_ids = {item.get("id") for item in spec.get("objects", [])}
        if not segments:
            issues.append(
                error("missing_segments", "spec must contain at least one segment", "segments")
            )
        self._validate_item_states(spec, object_ids, issues)
        previous_end_by_source: dict[str, float] = {}
        for index, segment in enumerate(segments):
            location = f"segments[{index}]"
            start = float(segment.get("start_seconds", -1))
            end = float(segment.get("end_seconds", -1))
            if end <= start:
                issues.append(
                    error("segment_time_invalid", "segment end must be after start", location)
                )
            source_video = str(segment.get("source_video") or spec.get("source_video") or "")
            previous_end = previous_end_by_source.get(source_video, -1.0)
            if start < previous_end:
                issues.append(
                    error("segment_time_overlap", "segment overlaps previous segment", location)
                )
            previous_end_by_source[source_video] = max(previous_end, end)
            for sop_id in segment.get("sop_ids", []):
                if sop_id not in sop_ids:
                    issues.append(error("missing_sop_ref", f"unknown SOP id {sop_id}", location))
            for item_id in segment.get("item_state_ids", []):
                if item_id not in item_state_ids:
                    issues.append(
                        error(
                            "missing_item_state_ref", f"unknown item state id {item_id}", location
                        )
                    )
            if not segment.get("action"):
                issues.append(
                    error("segment_action_missing", "segment action is required", location)
                )
        return ValidationReport(issues=issues)

    def _validate_item_states(
        self,
        spec: dict[str, Any],
        object_ids: set[str | None],
        issues: list[ValidationIssue],
    ) -> None:
        for index, item_state in enumerate(spec.get("item_states", [])):
            object_id = item_state.get("object_id")
            if object_id not in object_ids:
                issues.append(
                    error(
                        "missing_object_ref",
                        f"unknown object id {object_id}",
                        f"item_states[{index}]",
                    )
                )


def error(code: str, message: str, location: str) -> ValidationIssue:
    return ValidationIssue(severity="error", code=code, message=message, location=location)
