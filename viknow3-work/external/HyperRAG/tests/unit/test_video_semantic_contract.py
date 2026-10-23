from __future__ import annotations

from pathlib import Path

import hyperrag.hyperrag_v2 as hyperrag_v2_module
import pytest
from hyperrag.hyperrag_v2 import HyperRAGV2


def _sample_semantic() -> dict:
    return {
        "metadata": {
            "title": "设备检查演示",
            "semantic_type": "sop_video",
            "sources": [
                {
                    "source_id": "demo-video",
                    "source_type": "video",
                    "source_url": "/tmp/demo.mp4",
                }
            ],
        },
        "overview": {},
        "document_control": [],
        "groups": [],
        "segments": [
            {
                "segment_id": "demo-video_segment_0001",
                "video_id": "demo-video",
                "time_range": [1.0, 5.0],
                "data": {
                    "caption": "打开设备面板",
                    "brief_action": "打开面板",
                    "detailed_action": "操作员打开设备面板",
                    "concepts": ["设备面板"],
                    "segment_background": "设备检查",
                    "asr": {"text": "请打开面板", "segments": []},
                    "visual_text": {"ocr_keywords": ["POWER"], "ocr_text": "POWER OFF"},
                    "objects": [{"name": "设备面板", "description": "金属面板"}],
                    "actions": [{"subject": "操作员", "verb": "打开", "object": "设备面板"}],
                    "state_changes": ["关闭变为打开"],
                    "uncertainty": [],
                    "meta": {"video_name": "demo.mp4", "part_number": 1},
                },
            }
        ],
        "instructions": [
            {
                "step_id": "step-1",
                "action_flow": [
                    {
                        "action_id": "action-1",
                        "segment_ids": ["demo-video_segment_0001"],
                        "references": [],
                    }
                ],
            }
        ],
        "annotations": [
            {
                "annotation_id": "ann-1",
                "type": "highlight",
                "video_id": "demo-video",
                "time_range": [1.5, 2.5],
                "text_description": "重点查看开关",
                "data": {"shape": "box"},
            }
        ],
    }


def test_validate_video_semantic_payload_accepts_unified_schema() -> None:
    payload = HyperRAGV2._validate_video_semantic_payload(
        _sample_semantic(), semantic_type="sop_video"
    )

    assert payload["metadata"]["sources"][0]["source_id"] == "demo-video"
    assert payload["instructions"][0]["action_flow"][0]["segment_ids"] == [
        "demo-video_segment_0001"
    ]
    assert payload["annotations"][0]["annotation_id"] == "ann-1"


def test_validate_video_semantic_payload_accepts_legacy_key_objects() -> None:
    payload = _sample_semantic()
    legacy_objects = [{"category": "设备面板", "state": "关闭", "words": ""}]
    payload["segments"][0]["data"]["key_objects"] = legacy_objects

    result = HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")
    assert result["segments"][0]["data"]["key_objects"] == legacy_objects


def test_validate_video_semantic_payload_rejects_missing_video_id() -> None:
    payload = _sample_semantic()
    del payload["segments"][0]["video_id"]

    with pytest.raises(ValueError, match="video_id"):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")


@pytest.mark.parametrize(
    ("parse_mode", "include_new", "expected"),
    [("video", True, "new"), ("video", False, "legacy"), ("sop_video", True, "legacy")],
)
@pytest.mark.asyncio
async def test_video_semantic_filename_priority(
    tmp_path, monkeypatch, parse_mode, include_new, expected
):
    import json
    from types import SimpleNamespace

    root = tmp_path / "asset"
    root.mkdir()
    legacy = root / "sop-full.json"
    legacy.write_text(json.dumps({"marker": "legacy"}))
    original = legacy.read_bytes()
    if include_new:
        (root / "video-semantic.json").write_text(json.dumps({"marker": "new"}))

    class Storage:
        async def get_status(self, *_args):
            return SimpleNamespace(parse_mode=parse_mode)

    monkeypatch.setattr(hyperrag_v2_module, "get_asset_status_storage", lambda: Storage())
    rag = object.__new__(HyperRAGV2)
    rag._get_workspace_cache_dirs = lambda _workspace: (tmp_path, tmp_path)
    assert await rag.aget_video_semantic("ws", "asset") == {"marker": expected}
    assert legacy.read_bytes() == original


def test_get_video_semantic_paths_supports_plain_and_sop_video() -> None:
    rag = object.__new__(HyperRAGV2)
    rag._get_workspace_cache_dirs = lambda _workspace: (
        Path("/tmp/mineru"),
        Path("/tmp/video-cache"),
    )

    plain_primary, plain_candidates = rag._get_video_semantic_paths("demo", "asset-1", "video")
    sop_primary, sop_candidates = rag._get_video_semantic_paths("demo", "asset-2", "sop_video")

    assert plain_primary == Path("/tmp/video-cache/asset-1/video-semantic.json")
    assert plain_candidates[0] == plain_primary
    assert plain_candidates[1] == Path("/tmp/video-cache/asset-1/sop-full.json")
    assert sop_primary == Path("/tmp/video-cache/asset-2/sop-full.json")
    assert sop_candidates[0] == sop_primary
    assert sop_candidates[1] == Path("/tmp/video-cache/asset-2/actio_work/sop-full.json")


@pytest.mark.asyncio
async def test_aget_video_semantic_returns_transcript_from_sop_full(tmp_path, monkeypatch) -> None:
    workspace = "demo-workspace"
    asset_id = "asset-1"
    sop_full_dir = tmp_path / "video-cache" / asset_id
    sop_full_dir.mkdir(parents=True)
    transcript = [{"time": "0.00", "text": "开始操作"}]
    payload = {
        **_sample_semantic(),
        "transcript": transcript,
    }
    (sop_full_dir / "sop-full.json").write_text(
        __import__("json").dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    class _Status:
        parse_mode = "sop_video"

    class _Storage:
        async def get_status(self, ws: str, aid: str):
            assert ws == workspace
            assert aid == asset_id
            return _Status()

    monkeypatch.setattr(hyperrag_v2_module, "get_asset_status_storage", lambda: _Storage())

    rag = object.__new__(HyperRAGV2)
    rag._get_video_semantic_paths = lambda _workspace, _asset_id, _parse_mode: (
        sop_full_dir / "sop-full.json",
        [sop_full_dir / "sop-full.json"],
    )

    result = await rag.aget_video_semantic(workspace, asset_id)

    assert result is not None
    assert result["transcript"] == transcript


def test_validate_plain_video_rejects_instructions_even_when_empty() -> None:
    payload = _sample_semantic()
    payload["metadata"]["semantic_type"] = "video"
    payload["instructions"] = []

    with pytest.raises(ValueError, match="must not include instructions"):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="video")

    del payload["instructions"]
    normalized = HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="video")
    assert normalized["segments"][0]["segment_id"] == "demo-video_segment_0001"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption", None),
        ("segment_background", None),
        ("asr", None),
        ("visual_text", None),
        ("objects", None),
        ("actions", None),
        ("state_changes", None),
        ("uncertainty", None),
        ("meta", None),
    ],
)
def test_validate_video_semantic_payload_rejects_incomplete_segment_data(
    field: str, value: object
) -> None:
    payload = _sample_semantic()
    if value is None:
        del payload["segments"][0]["data"][field]

    with pytest.raises(ValueError, match=field):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")


@pytest.mark.parametrize("time_range", [["0", 5], [5, 1], [True, 5]])
def test_validate_video_semantic_payload_rejects_invalid_time_range(time_range) -> None:
    payload = _sample_semantic()
    payload["segments"][0]["time_range"] = time_range

    with pytest.raises(ValueError, match="time_range"):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("brief_action", []),
        ("detailed_action", []),
        ("concepts", {}),
    ],
)
def test_validate_video_semantic_payload_rejects_invalid_optional_segment_data(
    field: str, value: object
) -> None:
    payload = _sample_semantic()
    payload["segments"][0]["data"][field] = value

    with pytest.raises(ValueError, match=field):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")


def test_validate_sop_video_requires_explicit_segment_ids() -> None:
    payload = _sample_semantic()
    del payload["instructions"][0]["action_flow"][0]["segment_ids"]

    with pytest.raises(ValueError, match="segment_ids"):
        HyperRAGV2._validate_video_semantic_payload(payload, semantic_type="sop_video")
