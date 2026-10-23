from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


def video_semantic_paths(
    asset_root: Path, source_parser: str | None
) -> tuple[Path, list[Path]]:
    """Prefer the plain-video artifact; read historical SOP-named files without rewriting."""
    legacy = asset_root / "sop-full.json"
    if source_parser == "sop_video":
        return legacy, [legacy, asset_root / "actio_work" / "sop-full.json"]
    primary = asset_root / "video-semantic.json"
    return primary, [
        primary,
        legacy,
        asset_root / "actio_work" / "sop-full.json",
    ]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _format_object_text(value: Any) -> str:
    if isinstance(value, dict):
        obj_name = _clean_text(value.get("obj_name") or value.get("name") or value.get("category"))
        description = _clean_text(
            value.get("description") or value.get("state") or value.get("words")
        )
        if obj_name and description:
            return f"{obj_name}，{description}"
        return obj_name or description
    return _clean_text(value)


def _format_object_list(values: Any, *, limit: int = 8) -> List[str]:
    if not isinstance(values, list):
        return []
    cleaned = [_format_object_text(item) for item in values]
    return [item for item in cleaned if item][:limit]


def _clean_text_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [item for value in values if (item := _clean_text(value))]


def _focus_observation_text(value: Any) -> str:
    if not isinstance(value, dict) or value.get("observed") is not True:
        return ""
    description = _clean_text(value.get("description"))
    if not description:
        return ""
    start = value.get("start_value")
    end = value.get("end_value")
    if start is None and end is None:
        return ""

    def rendered(item: Any) -> str:
        if type(item) is bool:
            return "true" if item else "false"
        return _clean_text(item)

    start_text = rendered(start) if start is not None else ""
    end_text = rendered(end) if end is not None else ""
    if start_text and end_text and start != end:
        return f"{description} {start_text}→{end_text}"
    return f"{description} {end_text or start_text}"


def _focus_observation_list(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def _caption_data_visual_text(data: Dict[str, Any]) -> Dict[str, Any]:
    visual_text = data.get("visual_text")
    if not isinstance(visual_text, dict):
        visual_text = data.get("ocr")
    return visual_text if isinstance(visual_text, dict) else {}


def _caption_data_asr_text(data: Dict[str, Any]) -> str:
    asr = data.get("asr")
    if not isinstance(asr, dict):
        return ""
    text = _clean_text(asr.get("text"))
    if text:
        return text
    segments = asr.get("segments")
    if not isinstance(segments, list):
        return ""
    return " ".join(
        item
        for item in (
            _clean_text(segment.get("text")) for segment in segments if isinstance(segment, dict)
        )
        if item
    ).strip()


def build_caption_data_main_struct(
    caption_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    data = caption_data if isinstance(caption_data, dict) else {}
    caption = _clean_text(data.get("caption"))
    visual_text = _caption_data_visual_text(data)
    asr = data.get("asr")
    objects = data.get("object")
    if not isinstance(objects, list) or not objects:
        objects = data.get("objects")
    if not isinstance(objects, list) or not objects:
        objects = data.get("key_objects")
    if not isinstance(objects, list):
        objects = []

    result = {
        "caption": caption,
        "asr": dict(asr) if isinstance(asr, dict) else {},
        "ocr": {
            "ocr_keywords": _clean_text_list(
                visual_text.get("ocr_keywords") or data.get("ocr_keywords") or []
            ),
            "ocr_text": _clean_text(visual_text.get("ocr_text") or data.get("ocr_text")),
        },
        "object": list(objects),
        "focus_observations": _focus_observation_list(data.get("focus_observations")),
    }
    if data.get("claims_schema_version") is not None:
        result["claims_schema_version"] = data["claims_schema_version"]
        result["claims"] = list(data.get("claims") or [])
    return result


def format_caption_data_text_lines(caption_data: Optional[Dict[str, Any]]) -> List[str]:
    main_struct = build_caption_data_main_struct(caption_data)
    text_lines: List[str] = []

    caption = main_struct["caption"]
    if caption:
        text_lines.append(f"视频描述: {caption}")

    asr_text = _caption_data_asr_text(main_struct)
    if asr_text:
        text_lines.append(f"ASR: {asr_text}")

    ocr = main_struct["ocr"]
    ocr_text = ocr["ocr_text"] or "; ".join(ocr["ocr_keywords"])
    if ocr_text:
        text_lines.append(f"OCR: {ocr_text}")

    objects = _format_object_list(main_struct["object"])
    if objects:
        text_lines.append(f"主要对象: {'; '.join(objects)}")

    focus_values = [
        text
        for item in main_struct["focus_observations"]
        if (text := _focus_observation_text(item))
    ]
    if focus_values:
        text_lines.append(f"关注状态: {'；'.join(focus_values)}")

    return text_lines


def format_caption_data_text(caption_data: Optional[Dict[str, Any]]) -> str:
    return "\n".join(format_caption_data_text_lines(caption_data)).strip()


def build_video_chunk_display_metadata(main_struct: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Flatten a video's structured segment data for chunk metadata."""
    data = main_struct if isinstance(main_struct, dict) else {}
    normalized = build_caption_data_main_struct(data)
    ocr = normalized["ocr"]
    return {
        "caption": normalized["caption"],
        "object": "; ".join(_format_object_list(normalized["object"])),
        "ocr": ocr["ocr_text"] or "; ".join(ocr["ocr_keywords"]),
        "asr": _caption_data_asr_text(normalized),
    }


def get_primary_video_source(sop_data: Dict[str, Any]) -> Dict[str, str]:
    metadata = sop_data.get("metadata", {}) if isinstance(sop_data, dict) else {}
    for source in metadata.get("sources", []) or []:
        if source.get("source_type") == "video":
            return {
                "source_id": str(source.get("source_id") or ""),
                "source_url": str(source.get("source_url") or ""),
            }
    return {"source_id": "", "source_url": ""}


def build_video_segment_payload(
    *,
    segment_id: str,
    asset_id: str,
    video_source_id: str,
    source_parser: str,
    time_range: List[Any],
    caption_data: Optional[Dict[str, Any]] = None,
    order: int,
    instruction_links: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    data = caption_data if isinstance(caption_data, dict) else {}
    main_struct = build_caption_data_main_struct(data)
    text = format_caption_data_text(data)

    extra = {
        "asset_id": asset_id,
        "video_id": video_source_id or asset_id,
        "source_id": video_source_id or asset_id,
        "source_parser": source_parser,
        "caption_data": data,
    }
    links = instruction_links or []
    if links:
        extra["instruction_links"] = links
        extra["step_id"] = links[0]["step_id"]
        extra["action_id"] = links[0]["action_id"]
        extra["assertions"] = links[0]["assertions"]
    caption_metadata = data.get("caption_metadata")
    if isinstance(caption_metadata, dict):
        extra["caption_metadata"] = caption_metadata
    for key in ("technical_segment_id", "slide_id", "slide_start", "slide_end"):
        if data.get(key) is not None:
            extra[key] = data[key]

    return {
        "segment_id": segment_id,
        "doc_id": asset_id,
        "order": order,
        "type": "video_clip",
        "text": text,
        "main_struct": main_struct,
        "location": {
            "begin_ts": time_range[0],
            "end_ts": time_range[1],
        },
        "extra": extra,
    }


def build_video_segments(
    *,
    asset_id: str,
    sop_full_data: Dict[str, Any],
    source_parser: str,
) -> List[Dict[str, Any]]:
    video_source = get_primary_video_source(sop_full_data)
    raw_segments = sop_full_data.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("video semantic payload must include segments")

    segment_by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            raise ValueError("segments entries must be dict")
        segment_id = str(segment.get("segment_id") or "")
        if not segment_id or segment_id in segment_by_id:
            raise ValueError("segments must have unique non-empty segment_id")
        time_range = segment.get("time_range")
        if not isinstance(time_range, list) or len(time_range) != 2:
            raise ValueError("segments must include [begin, end] time_range")
        if not isinstance(segment.get("data"), dict):
            raise ValueError("segments must include data dict")
        segment_by_id[segment_id] = segment
        ordered_ids.append(segment_id)

    links_by_segment: Dict[str, List[Dict[str, Any]]] = {}
    if source_parser == "sop_video":
        instructions = sop_full_data.get("instructions")
        if not isinstance(instructions, list):
            raise ValueError("sop_video payload must include instructions")
        for step in instructions:
            if not isinstance(step, dict):
                raise ValueError("instructions entries must be dict")
            step_id = str(step.get("step_id") or "")
            for action in step.get("action_flow", []) or []:
                if not isinstance(action, dict):
                    raise ValueError("action_flow entries must be dict")
                action_id = str(action.get("action_id") or "")
                if "segment_ids" not in action:
                    raise ValueError("action_flow[].segment_ids is required")
                segment_ids = action["segment_ids"]
                if not isinstance(segment_ids, list):
                    raise ValueError("action_flow[].segment_ids must be a list")
                for segment_id_value in segment_ids:
                    segment_id = str(segment_id_value)
                    if segment_id not in segment_by_id:
                        raise ValueError(f"unknown segment_id in action_flow: {segment_id}")
                    links_by_segment.setdefault(segment_id, []).append(
                        {
                            "step_id": step_id,
                            "action_id": action_id,
                            "assertions": action.get("assertions", []),
                        }
                    )
        selected_ids = [segment_id for segment_id in ordered_ids if segment_id in links_by_segment]
    elif source_parser == "video":
        selected_ids = ordered_ids
    else:
        raise ValueError(f"unsupported video source_parser: {source_parser}")

    return [
        build_video_segment_payload(
            segment_id=segment_id,
            asset_id=asset_id,
            video_source_id=str(
                segment_by_id[segment_id].get("video_id") or video_source["source_id"]
            ),
            source_parser=source_parser,
            time_range=segment_by_id[segment_id]["time_range"],
            caption_data=segment_by_id[segment_id]["data"],
            order=order,
            instruction_links=links_by_segment.get(segment_id),
        )
        for order, segment_id in enumerate(selected_ids)
    ]
