from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _complete_data(caption: str) -> dict:
    return {
        "caption": caption,
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


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_video_semantic.py"
    spec = importlib.util.spec_from_file_location("migrate_video_semantic", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migrate_video_cache_builds_plain_video_semantic(tmp_path: Path) -> None:
    module = _load_script_module()
    cache_dir = tmp_path / "asset-1"
    cache_dir.mkdir()
    (cache_dir / "index.json").write_text(
        json.dumps(
            {
                "asset": {
                    "doc_id": "demo-video",
                    "file_path": "/tmp/demo.mp4",
                    "title": "演示视频",
                },
                "segments": [
                    {
                        "summary": "打开设备面板",
                        "location": {"begin_ts": 1.0, "end_ts": 5.0},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrated = module.migrate_video_cache(tmp_path, write=True)

    assert migrated == [cache_dir / "video-semantic.json"]
    payload = json.loads((cache_dir / "video-semantic.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["semantic_type"] == "video"
    assert payload["metadata"]["sources"][0]["source_id"] == "demo-video"
    assert payload["segments"] == [
        {
            "segment_id": "demo-video_segment_0001",
            "video_id": "demo-video",
            "time_range": [1.0, 5.0],
            "data": _complete_data("打开设备面板"),
        }
    ]
    assert "instructions" not in payload
    assert payload["annotations"] == []


def test_migrate_video_cache_flattens_legacy_sop_video_payload(tmp_path: Path) -> None:
    module = _load_script_module()
    cache_dir = tmp_path / "asset-2" / "actio_work"
    cache_dir.mkdir(parents=True)
    (cache_dir / "sop-full.json").write_text(
        json.dumps(
            {
                "metadata": {"title": "SOP"},
                "instructions": [
                    {
                        "step_id": "step-1",
                        "action_flow": [
                            {
                                "action_id": "action-1",
                                "references": [
                                    {
                                        "type": "video",
                                        "video_id": "video-1",
                                        "time_range": [1.0, 5.0],
                                        "data": {"caption": "打开设备面板"},
                                    },
                                    {"type": "document", "document_id": "manual"},
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrated = module.migrate_video_cache(tmp_path, write=True)

    assert migrated == [tmp_path / "asset-2" / "sop-full.json"]
    payload = json.loads((tmp_path / "asset-2" / "sop-full.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["semantic_type"] == "sop_video"
    assert payload["metadata"]["sources"][0]["source_id"] == "asset-2"
    assert payload["segments"] == [
        {
            "segment_id": "video-1_segment_0001",
            "video_id": "video-1",
            "time_range": [1.0, 5.0],
            "data": _complete_data("打开设备面板"),
        }
    ]
    action = payload["instructions"][0]["action_flow"][0]
    assert action["segment_ids"] == ["video-1_segment_0001"]
    assert action["references"] == [{"type": "document", "document_id": "manual"}]
    assert payload["document_control"] == []
    assert payload["groups"] == []
    assert payload["annotations"] == []


def _write_legacy(root):
    asset = root / "asset"
    legacy = asset / "actio_work" / "sop-full.json"
    legacy.parent.mkdir(parents=True)
    payload = {
        "metadata": {"title": "操作原文"},
        "instructions": [],
        "transcript": [{"text": "请保留完整原句。", "begin_ts": 0, "end_ts": 5}],
    }
    legacy.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return legacy, asset / "sop-full.json", payload


def test_migration_defaults_to_dry_run_and_is_idempotent(tmp_path):
    module = _load_script_module()
    legacy, target, original = _write_legacy(tmp_path)
    before = legacy.read_bytes()
    assert module.migrate_video_cache(tmp_path) == [target]
    assert not target.exists()
    assert module.migrate_video_cache(tmp_path, write=True) == [target]
    result = json.loads(target.read_text())
    assert result["transcript"] == original["transcript"]
    assert result["metadata"]["title"] == original["metadata"]["title"]
    assert result["metadata"]["semantic_type"] == "sop_video"
    assert result["segments"] == []
    assert legacy.read_bytes() == before
    written = target.read_bytes()
    assert module.migrate_video_cache(tmp_path, write=True) == []
    assert target.read_bytes() == written


def test_migration_preserves_existing_root_even_when_legacy_is_invalid(tmp_path):
    module = _load_script_module()
    legacy, target, _ = _write_legacy(tmp_path)
    target.write_text('{"published": "existing original"}', encoding="utf-8")
    legacy.write_text("invalid json", encoding="utf-8")
    original = target.read_bytes()
    assert module.migrate_video_cache(tmp_path, write=True) == []
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", '{"asset": {}, "segments": "invalid"}', '{"asset": {}, "segments": [null]}'],
)
def test_migration_rejects_invalid_input_without_writing(tmp_path, payload):
    module = _load_script_module()
    asset = tmp_path / "bad"
    asset.mkdir()
    (asset / "index.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="index.json"):
        module.migrate_video_cache(tmp_path, write=True)
    assert not (asset / "sop-full.json").exists()


def test_migration_rejects_malformed_legacy_semantics(tmp_path):
    module = _load_script_module()
    legacy, target, _ = _write_legacy(tmp_path)
    legacy.write_text('{"metadata": {}, "instructions": "bad"}', encoding="utf-8")
    with pytest.raises(ValueError, match="sop-full.json"):
        module.migrate_video_cache(tmp_path, write=True)
    assert not target.exists()


@pytest.mark.parametrize("modality", ["document", "audio", "image"])
def test_migration_ignores_explicit_non_video_indexes(tmp_path, modality):
    module = _load_script_module()
    asset = tmp_path / "non-video"
    asset.mkdir()
    index = asset / "index.json"
    index.write_text(
        json.dumps({"asset": {"doc_id": "non-video", "modality": modality}, "segments": []}),
        encoding="utf-8",
    )
    original = index.read_bytes()
    assert module.migrate_video_cache(tmp_path) == []
    assert module.migrate_video_cache(tmp_path, write=True) == []
    assert not (asset / "sop-full.json").exists()
    assert index.read_bytes() == original


def test_migration_validates_entire_batch_before_writing(tmp_path):
    module = _load_script_module()
    _, valid_target, _ = _write_legacy(tmp_path)
    invalid = tmp_path / "z-invalid"
    invalid.mkdir()
    (invalid / "index.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="z-invalid/index.json"):
        module.migrate_video_cache(tmp_path, write=True)
    assert not valid_target.exists()
    assert not (invalid / "sop-full.json").exists()


def test_migration_preserves_concurrent_publication_and_cleans_temp(tmp_path, monkeypatch):
    module = _load_script_module()
    _, target, _ = _write_legacy(tmp_path)
    original = b'{"published": "concurrent writer"}'

    def publish_first(source, destination):
        assert destination == target
        assert json.loads(Path(source).read_text())["transcript"]
        destination.write_bytes(original)
        raise FileExistsError(str(destination))

    monkeypatch.setattr(module.os, "link", publish_first)
    assert module.migrate_video_cache(tmp_path, write=True) == []
    assert target.read_bytes() == original
    assert {path.name for path in target.parent.iterdir()} == {"actio_work", "sop-full.json"}


def test_migration_preserves_all_segment_data_fields(tmp_path: Path) -> None:
    module = _load_script_module()
    cache_dir = tmp_path / "asset-lossless" / "actio_work"
    cache_dir.mkdir(parents=True)
    legacy_data = {
        "caption": "open panel",
        "brief_action": "open",
        "detailed_action": "open the panel",
        "concepts": ["panel"],
        "segment_background": "inspection context",
        "asr": {"text": "open the panel", "segments": []},
        "visual_text": {"ocr_keywords": ["POWER"], "ocr_text": "POWER OFF"},
        "key_objects": [{"category": "panel", "state": "closed", "words": ""}],
        "objects": [
            {
                "name": "panel",
                "appearance": "metal",
                "role": "target",
                "position": "center",
                "description": "metal panel",
            }
        ],
        "actions": [{"subject": "operator", "verb": "open", "object": "panel"}],
        "state_changes": ["closed to open"],
        "uncertainty": [],
        "meta": {
            "video_name": "video-1.mp4",
            "start_time": 1.0,
            "end_time": 5.0,
            "part_number": 1,
        },
        "caption_metadata": {"caption_status": "completed"},
    }
    (cache_dir / "sop-full.json").write_text(
        json.dumps(
            {
                "metadata": {"title": "SOP"},
                "instructions": [
                    {
                        "step_id": "step-1",
                        "action_flow": [
                            {
                                "action_id": "action-1",
                                "references": [
                                    {
                                        "type": "video",
                                        "video_id": "video-1",
                                        "time_range": [1.0, 5.0],
                                        "data": legacy_data,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    module.migrate_video_cache(tmp_path, write=True)

    payload = json.loads(
        (tmp_path / "asset-lossless" / "sop-full.json").read_text(encoding="utf-8")
    )
    assert payload["segments"][0]["data"] == legacy_data
    assert payload["segments"][0]["data"]["objects"] != legacy_data["key_objects"]
