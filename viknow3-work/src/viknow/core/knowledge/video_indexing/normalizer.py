"""Normalize extracted video slices into the published video index spec shape."""

from __future__ import annotations

from typing import Any

from viknow.core.knowledge.video_indexing.domain import (
    SegmentDescription,
    SemanticSlice,
    SopAlignment,
    SopStep,
)


class SpecNormalizer:
    """Convert slice descriptions and optional SOP alignments to KB spec JSON."""

    def __init__(self, *, index_id: str, source_video: str, source_index: str = "") -> None:
        self.index_id = index_id
        self.source_video = source_video
        self.source_index = source_index

    def normalize(
        self,
        *,
        slices: list[SemanticSlice],
        descriptions: list[SegmentDescription],
        sop_steps: list[SopStep],
        alignments: list[SopAlignment],
    ) -> dict[str, Any]:
        descriptions_by_slice = {item.slice_id: item for item in descriptions}
        alignments_by_slice = {item.slice_id: item for item in alignments}
        object_ids: dict[str, str] = {}
        item_states: dict[str, dict[str, str]] = {}
        segments = []
        for index, slice_item in enumerate(slices, start=1):
            description = descriptions_by_slice[slice_item.id]
            item_state_ids = []
            for object_label in description.objects:
                object_ids.setdefault(object_label, f"Object_{safe_id(object_label)}")
            for state_text in description.states:
                object_label = pick_object_for_state(state_text, list(object_ids))
                object_id = object_ids.setdefault(object_label, f"Object_{safe_id(object_label)}")
                item_id = f"Item_{safe_id(state_text)}"
                item_states[item_id] = {
                    "id": item_id,
                    "text": state_text if state_text.endswith("。") else f"{state_text}。",
                    "object_id": object_id,
                }
                item_state_ids.append(item_id)
            alignment = alignments_by_slice.get(slice_item.id)
            segments.append(
                {
                    "id": f"Seg_{index}",
                    "title": description.title,
                    "start_seconds": int(round(slice_item.start_seconds)),
                    "end_seconds": int(round(slice_item.end_seconds)),
                    "action": description.action,
                    "semantic": description.semantic,
                    "sop_ids": alignment.sop_ids if alignment else [],
                    "item_state_ids": item_state_ids,
                }
            )
        return {
            "index_id": self.index_id,
            "source_video": self.source_video,
            "source_index": self.source_index,
            "sop_document": sop_steps[0].document_uri if sop_steps else "",
            "segments": segments,
            "sop_steps": [step.model_dump(mode="json") for step in sop_steps],
            "objects": [
                {"id": object_id, "label": label}
                for label, object_id in sorted(object_ids.items(), key=lambda item: item[1])
            ],
            "item_states": list(item_states.values()),
        }


def pick_object_for_state(state_text: str, object_labels: list[str]) -> str:
    for label in object_labels:
        if label in state_text:
            return label
    return object_labels[0] if object_labels else "未知对象"


def safe_id(value: str) -> str:
    return (
        value.strip()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("（", "_")
        .replace("）", "_")
        .replace("(", "_")
        .replace(")", "_")
        .strip("_")
    )
