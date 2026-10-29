"""Migrate legacy video cache artifacts; dry-run unless --write is supplied."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from hyperrag.hyperrag_v2 import HyperRAGV2


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid video cache {path}: {exc}") from exc


def _metadata(payload: dict, *, asset_id: str, semantic_type: str) -> dict:
    metadata = dict(payload.get("metadata") or {})
    metadata["semantic_type"] = semantic_type
    if not metadata.get("sources"):
        metadata["sources"] = [{"source_id": asset_id, "source_type": "video", "source_url": ""}]
    return metadata


def _complete_segment_data(data: dict) -> dict:
    completed = dict(data)
    defaults = {
        "caption": "",
        "segment_background": "",
        "asr": {},
        "visual_text": {},
        "key_objects": [],
        "objects": [],
        "actions": [],
        "state_changes": [],
        "uncertainty": [],
        "meta": {},
    }
    for key, value in defaults.items():
        completed.setdefault(key, value)
    return completed


def _segment_data(segment: dict) -> dict:
    extra = segment.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("caption_data"), dict):
        return _complete_segment_data(extra["caption_data"])
    data = segment.get("data")
    if isinstance(data, dict):
        return _complete_segment_data(data)
    caption = segment.get("summary") or segment.get("text") or ""
    return _complete_segment_data({"caption": str(caption)} if caption else {})


def _plain_semantic_from_index(asset_dir: Path, payload: dict) -> dict | None:
    asset = payload.get("asset")
    segments = payload.get("segments")
    if not isinstance(asset, dict):
        raise ValueError("index requires an asset object")
    if asset.get("modality") not in (None, "", "video"):
        return None
    if not isinstance(segments, list):
        raise ValueError("index requires a segments list")

    asset_id = str(asset.get("doc_id") or asset_dir.name)
    normalized_segments = []
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise ValueError("segments entries must be objects")
        location = segment.get("location")
        if not isinstance(location, dict):
            raise ValueError("segments entries must include location")
        normalized_segments.append(
            {
                "segment_id": str(segment.get("segment_id") or f"{asset_id}_segment_{index:04d}"),
                "video_id": asset_id,
                "time_range": [location.get("begin_ts"), location.get("end_ts")],
                "data": _segment_data(segment),
            }
        )

    transcript_path = asset_dir / "transcript.json"
    transcript = _read_json(transcript_path) if transcript_path.exists() else None
    if transcript is not None and not isinstance(transcript, (list, dict)):
        raise ValueError("transcript must be a list or object")
    result = {
        "metadata": {
            "semantic_type": "video",
            "title": asset.get("title") or "",
            "sources": [
                {
                    "source_id": asset_id,
                    "source_type": "video",
                    "source_url": str(asset.get("source_uri") or asset.get("file_path") or ""),
                }
            ],
        },
        "segments": normalized_segments,
        "annotations": [],
    }
    if transcript is not None:
        result["transcript"] = transcript
    return result


def _legacy_sop_semantic(asset_dir: Path, payload: dict) -> dict:
    instructions = payload.get("instructions")
    if not isinstance(instructions, list):
        raise ValueError("instructions must be a list")

    result = dict(payload)
    result["metadata"] = _metadata(payload, asset_id=asset_dir.name, semantic_type="sop_video")
    segments = result.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("segments entries must be objects")
            data = segment.get("data")
            if not isinstance(data, dict):
                raise ValueError("segments entries must include data object")
            segment["data"] = _complete_segment_data(data)
        return result
    if segments is not None:
        raise ValueError("segments must be a list")

    result["segments"] = []
    seen_ids = set()
    counter = 0
    normalized_instructions = []
    for step in instructions:
        if not isinstance(step, dict):
            raise ValueError("instructions entries must be objects")
        normalized_step = dict(step)
        normalized_actions = []
        action_flow = step.get("action_flow", [])
        if not isinstance(action_flow, list):
            raise ValueError("action_flow must be a list")
        for action in action_flow:
            if not isinstance(action, dict):
                raise ValueError("action_flow entries must be objects")
            normalized_action = dict(action)
            segment_ids = list(action.get("segment_ids") or [])
            document_references = []
            references = action.get("references", [])
            if not isinstance(references, list):
                raise ValueError("references must be a list")
            for reference in references:
                if not isinstance(reference, dict):
                    raise ValueError("references entries must be objects")
                if reference.get("type") != "video":
                    document_references.append(reference)
                    continue
                counter += 1
                video_id = str(reference.get("video_id") or asset_dir.name)
                segment_id = str(reference.get("segment_id") or f"{video_id}_segment_{counter:04d}")
                if segment_id in seen_ids:
                    raise ValueError(f"duplicate video segment_id: {segment_id}")
                seen_ids.add(segment_id)
                time_range = reference.get("time_range")
                if not isinstance(time_range, list) or len(time_range) != 2:
                    raise ValueError("video references must include [begin, end] time_range")
                data = reference.get("data")
                if not isinstance(data, dict):
                    raise ValueError("video references must include data object")
                result["segments"].append(
                    {
                        "segment_id": segment_id,
                        "video_id": video_id,
                        "time_range": time_range,
                        "data": _complete_segment_data(data),
                    }
                )
                segment_ids.append(segment_id)
            normalized_action["segment_ids"] = segment_ids
            normalized_action["references"] = document_references
            normalized_actions.append(normalized_action)
        normalized_step["action_flow"] = normalized_actions
        normalized_instructions.append(normalized_step)
    result["instructions"] = normalized_instructions
    return result


def _semantic_from_cache(asset_dir: Path, source: Path) -> dict | None:
    try:
        payload = _read_json(source)
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        if source.name == "index.json":
            payload = _plain_semantic_from_index(asset_dir, payload)
            if payload is None:
                return None
        else:
            payload = _legacy_sop_semantic(asset_dir, payload)
        semantic_type = payload["metadata"]["semantic_type"]
        return HyperRAGV2._validate_video_semantic_payload(payload, semantic_type=semantic_type)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid video cache {source}: {exc}") from exc


def migrate_video_cache(cache_root: Path, *, write: bool = False) -> list[Path]:
    """Return planned/written destinations; never overwrite an existing root artifact."""
    cache_root = Path(cache_root)
    if not cache_root.is_dir():
        raise ValueError(f"Video cache directory does not exist: {cache_root}")
    pending = []
    for asset_dir in sorted(cache_root.iterdir()):
        if not asset_dir.is_dir() or asset_dir.is_symlink():
            continue
        if any(
            path.exists() or path.is_symlink()
            for path in (asset_dir / "video-semantic.json", asset_dir / "sop-full.json")
        ):
            continue
        legacy = asset_dir / "actio_work" / "sop-full.json"
        index = asset_dir / "index.json"
        source = legacy if legacy.exists() else index
        if not source.exists():
            continue
        payload = _semantic_from_cache(asset_dir, source)
        if payload is not None:
            filename = (
                "sop-full.json"
                if payload["metadata"]["semantic_type"] == "sop_video"
                else "video-semantic.json"
            )
            target = asset_dir / filename
            pending.append((target, payload))

    # Validate every candidate before writing any part of the batch.
    migrated = []
    for target, payload in pending:
        if write:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                # Atomic no-clobber publication also protects a concurrently published artifact.
                os.link(temporary, target)
            except FileExistsError:
                continue
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        migrated.append(target)
    return migrated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache_root", type=Path)
    parser.add_argument(
        "--write", action="store_true", help="Write validated results (default: dry-run)"
    )
    args = parser.parse_args()
    for path in migrate_video_cache(args.cache_root, write=args.write):
        print(path)


if __name__ == "__main__":
    main()
