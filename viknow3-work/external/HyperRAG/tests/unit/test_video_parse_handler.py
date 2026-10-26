from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hyperrag.task.handler import VideoParseHandler
from hyperrag.video_media import build_video_embedding_text
from hyperrag.task.models import Task, TaskResource, TaskStatus


class _ArtifactStoreStub:
    def __init__(self, root: Path) -> None:
        self._root = root

    def get_video_cache_root(self, workspace: str) -> Path:
        return self._root / workspace


def _build_task(video_path: Path, extra_metadata: dict | None = None) -> Task:
    metadata = {
        "asset_path": str(video_path),
        "language": "zh",
        "title": "演示视频",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return Task(
        task_id="task-1",
        task_type="video_parse",
        workspace="ws",
        asset_id="asset-1",
        status=TaskStatus.PENDING,
        resource=TaskResource(),
        metadata=metadata,
    )


def test_build_video_embedding_text_uses_caption_and_ocr() -> None:
    text = build_video_embedding_text(
        {
            "text": "动作: 打开设备面板",
            "location": {"begin_ts": 0.0, "end_ts": 3.0},
            "extra": {
                "caption_data": {
                    "caption": "画面显示操作员打开设备面板。",
                    "ocr_keywords": ["SX-70", "POWER"],
                }
            },
        }
    )

    assert "<caption>画面显示操作员打开设备面板。 OCR: SX-70; POWER</caption>" in text
    assert "<ocr_keywords>SX-70；POWER</ocr_keywords>" in text
    assert "动作: 打开设备面板" not in text
    assert "ASR:" not in text


def test_build_video_embedding_text_uses_nested_visual_text_ocr() -> None:
    text = build_video_embedding_text(
        {
            "extra": {
                "caption_data": {
                    "caption": "画面显示操作员打开设备面板。",
                    "visual_text": {
                        "ocr_keywords": ["SX-70", "POWER"],
                        "ocr_text": "POWER ON",
                    },
                }
            },
        }
    )

    assert "<caption>画面显示操作员打开设备面板。 OCR: POWER ON</caption>" in text
    assert "<ocr_keywords>SX-70；POWER</ocr_keywords>" in text


def test_build_video_embedding_text_prefers_main_struct() -> None:
    text = build_video_embedding_text(
        {
            "main_struct": {
                "caption": "操作员打开设备面板。",
                "asr": {"text": "请打开面板", "segments": []},
                "ocr": {
                    "ocr_keywords": ["POWER"],
                    "ocr_text": "POWER ON",
                },
                "object": [{"category": "设备面板", "state": "关闭"}],
            },
            "extra": {
                "caption_data": {
                    "caption": "不应使用的旧描述",
                    "state_changes": ["不应进入向量文本"],
                }
            },
        }
    )

    assert (
        "<caption>操作员打开设备面板。 ASR: 请打开面板 OCR: POWER ON "
        "主要对象: 设备面板，关闭</caption>"
    ) in text


@pytest.mark.parametrize(
    "caption_outcome", ["completed", "partial_failed", "completed_with_warnings"]
)
@pytest.mark.asyncio
async def test_video_parse_handler_writes_unified_sop_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caption_outcome: str
) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_text("video", encoding="utf-8")

    sop_full = _plain_video_sop_full(video_path)
    stats = {
        "total": 2,
        "succeeded": 1 if caption_outcome == "partial_failed" else 2,
        "failed": 1 if caption_outcome == "partial_failed" else 0,
        "failed_nodes": [{"node_id": "bad", "retry_error": "request rejected"}]
        if caption_outcome == "partial_failed"
        else [],
    }
    warnings = ["观察计划可能过于泛化"] if caption_outcome != "completed" else []
    sop_full["metadata"].update(
        caption_stats=stats,
        indexing_outcome=caption_outcome,
        global_context_quality_warnings=warnings,
    )
    parser_configs = []
    parse_calls = []

    class FakeActioVideoParser:
        def __init__(self, config):
            parser_configs.append(config)

        def parse_video_only(self, parsed_video_path, *, video_id, title, output_path=None):
            parse_calls.append(
                {
                    "video_path": parsed_video_path,
                    "video_id": video_id,
                    "title": title,
                    "output_path": output_path,
                }
            )
            if output_path is not None:
                Path(output_path).write_text(
                    json.dumps(sop_full, ensure_ascii=False),
                    encoding="utf-8",
                )
            return SimpleNamespace(
                merged_sop=sop_full,
                segment_output={"metadata": [{"duration": 95.5}]},
            )

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", FakeActioVideoParser)
    monkeypatch.setattr(
        "hyperrag.task.handler.ActioVideoParserConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    handler = VideoParseHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"),
        actio_config={
            "models_config_path": str(tmp_path / "model_calls.yaml"),
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_config": str(tmp_path / "merge.yaml"),
            "annotate_max_concurrency": 2,
            "device": "cpu",
        },
    )

    result = await handler(_build_task(video_path))

    assert result.status == "completed"
    assert result.data is not None

    sop_full_path = tmp_path / "cache" / "ws" / "asset-1" / "video-semantic.json"
    assert sop_full_path.exists()
    assert not sop_full_path.with_name("sop-full.json").exists()

    payload = json.loads(sop_full_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["title"] == "演示视频"
    assert payload["metadata"]["sources"][0]["source_id"] == "asset-1"
    assert "instructions" not in payload
    assert result.data["segments"][0]["text"] == ""
    assert "SOP:" not in result.data["segments"][0]["text"]
    assert "步骤:" not in result.data["segments"][0]["text"]
    assert result.data["asset"]["extra"]["source_parser"] == "video"
    assert result.data["asset"]["extra"]["duration_sec"] == 95.5
    assert result.data["segments"][0]["main_struct"]["caption"] == ""
    assert result.data["asset"]["extra"]["caption_stats"] == stats
    assert result.data["asset"]["extra"]["indexing_outcome"] == caption_outcome
    assert result.data["asset"]["extra"]["global_context_quality_warnings"] == warnings
    index_content = json.loads(result.data["content"])
    assert index_content["asset"]["extra"]["caption_stats"] == stats
    assert (
        index_content["segments"][0]["main_struct"] == (result.data["segments"][0]["main_struct"])
    )
    assert parse_calls == [
        {
            "video_path": video_path,
            "video_id": "asset-1",
            "title": "演示视频",
            "output_path": sop_full_path,
        }
    ]
    assert parser_configs[0].models_config == str(tmp_path / "model_calls.yaml")
    assert parser_configs[0].segment_config == str(tmp_path / "segment.yaml")
    assert parser_configs[0].device == "cpu"
    assert parser_configs[0].annotate_max_concurrency == 2


@pytest.mark.asyncio
async def test_video_parse_handler_passes_edited_sop_path_for_update_caption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_text("video", encoding="utf-8")
    edited_sop_path = tmp_path / "edited-sop-full.json"
    edited_sop_path.write_text(
        json.dumps({"metadata": {"global_summary": "已编辑摘要"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    sop_full = _plain_video_sop_full(video_path)
    parse_calls = []

    class FakeActioVideoParser:
        def __init__(self, config):
            del config

        def parse_video_only(
            self,
            parsed_video_path,
            *,
            video_id,
            title,
            edited_sop_full_path=None,
            output_path=None,
        ):
            parse_calls.append(
                {
                    "video_path": parsed_video_path,
                    "video_id": video_id,
                    "title": title,
                    "edited_sop_full_path": edited_sop_full_path,
                    "output_path": output_path,
                }
            )
            if output_path is not None:
                Path(output_path).write_text(
                    json.dumps(sop_full, ensure_ascii=False), encoding="utf-8"
                )
            return SimpleNamespace(merged_sop=sop_full)

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", FakeActioVideoParser)
    monkeypatch.setattr(
        "hyperrag.task.handler.ActioVideoParserConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    handler = VideoParseHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"),
        actio_config={
            "models_config_path": str(tmp_path / "model_calls.yaml"),
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_config": str(tmp_path / "merge.yaml"),
        },
    )
    task = _build_task(
        video_path,
        extra_metadata={"edited_sop_full_path": str(edited_sop_path)},
    ).model_copy(update={"task_type": "update_caption"})

    result = await handler(task)

    assert result.status == "completed"
    assert parse_calls[0]["edited_sop_full_path"] == edited_sop_path


@pytest.mark.asyncio
async def test_video_parse_handler_keeps_terminology_context_in_actio_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_text("video", encoding="utf-8")

    sop_full = _plain_video_sop_full(video_path)

    class FakeActioVideoParser:
        def __init__(self, config):
            del config

        def parse_video_only(self, parsed_video_path, *, video_id, title, output_path=None):
            del parsed_video_path, video_id, title, output_path
            return SimpleNamespace(merged_sop=sop_full)

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", FakeActioVideoParser)
    monkeypatch.setattr(
        "hyperrag.task.handler.ActioVideoParserConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    terminology_helper = Mock()
    terminology_helper.resolve_terminology_full_docs = AsyncMock(
        return_value=(["term-1"], [{"content": "ignored"}])
    )
    terminology_helper.format_terminology_prompt_context.return_value = "术语：阻门器"

    handler = VideoParseHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"),
        terminology_helper=terminology_helper,
        actio_config={
            "models_config_path": str(tmp_path / "model_calls.yaml"),
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_config": str(tmp_path / "merge.yaml"),
        },
    )

    result = await handler(_build_task(video_path))

    assert result.status == "completed"
    assert result.data is not None
    assert result.data["sop_full"]["metadata"]["terminology_context"] == "术语：阻门器"


def _plain_video_sop_full(video_path: Path) -> dict:
    return {
        "metadata": {
            "title": "演示视频",
            "sources": [
                {
                    "semantic_type": "video",
                    "source_id": "asset-1",
                    "source_type": "video",
                    "source_url": str(video_path),
                }
            ],
        },
        "overview": {
            "purpose": "演示操作",
            "scope": "",
            "safety": "",
            "parts_required": [],
            "tools_required": [],
        },
        "document_control": [],
        "groups": [],
        "segments": [
            {
                "segment_id": "asset-1_segment_0001",
                "video_id": "asset-1",
                "time_range": [0.0, 3.0],
                "data": {},
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_coach_background_is_forwarded_from_index_parameters(tmp_path, monkeypatch, nested):
    path = tmp_path / "demo.mp4"
    path.write_bytes(b"video")
    payload = _plain_video_sop_full(path)
    captured = []
    parser_configs = []

    class Parser:
        def __init__(self, config):
            parser_configs.append(config)

        def parse_video_only(self, path, **kwargs):
            captured.append(kwargs)
            Path(kwargs["output_path"]).write_text(json.dumps(payload))
            return SimpleNamespace(merged_sop=payload)

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", Parser)
    monkeypatch.setattr(
        "hyperrag.task.handler.ActioVideoParserConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    handler = VideoParseHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"),
        actio_config={
            "models_config_path": str(tmp_path / "model.yaml"),
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_config": str(tmp_path / "merge.yaml"),
        },
    )
    background = {
        "introduction": "optics",
        "learning_goals": "understand etching",
        "audience": "students",
    }
    params = {"course_background": background, "segmentation_mode": "presentation"}
    result = await handler(_build_task(path, {"params": params} if nested else params))
    assert result.status == "completed"
    assert captured[0]["course_background"] == background
    assert parser_configs[0].segmentation_mode == "presentation"


def test_global_candidates_never_enter_segment_embedding_without_local_evidence():
    segment = {
        "global_context": {"observation_focus": [{"target": "记分牌", "aspects": ["比分"]}]},
        "extra": {
            "global_context": {"observation_focus": [{"target": "记分牌", "aspects": ["比分"]}]},
            "caption_data": {"caption": "观众在看台上挥手。"},
        },
    }
    assert "比分" not in build_video_embedding_text(segment)
    segment["extra"]["caption_data"]["state_changes"] = ["记分牌比分由 1:0 变为 2:0"]
    assert "2:0" not in build_video_embedding_text(segment)


@pytest.mark.asyncio
async def test_summary_storage_preserves_omitted_context_and_updates_explicit_context(
    tmp_path, monkeypatch
):
    import hyperrag.hyperrag_v2 as module
    from hyperrag.hyperrag_v2 import HyperRAGV2

    def _normalize(value):
        if not isinstance(value, dict):
            raise ValueError("global_context must be an object")
        focus = value.get("observation_focus")
        if focus is not None and not isinstance(focus, list):
            raise ValueError("observation_focus must be a list")
        return value

    monkeypatch.setattr("hyperrag.actio_port.normalize_global_context", _normalize)

    path = tmp_path / "sop-full.json"
    original = {"keywords": ["电梯"], "attention_items": []}
    path.write_text(
        json.dumps({"metadata": {"global_summary": "旧摘要", "global_context": original}})
    )
    status = SimpleNamespace(parse_mode="video")
    storage = SimpleNamespace(get_status=AsyncMock(return_value=status))
    monkeypatch.setattr(module, "get_asset_status_storage", lambda: storage)
    app = object.__new__(HyperRAGV2)
    app._get_video_semantic_paths = Mock(return_value=(path, [path]))
    app.aupdate_video_index = AsyncMock(return_value="job-1")
    assert await app.aupdate_video_global_summary("ws", "asset", "新摘要") == "job-1"
    assert json.loads(path.read_text())["metadata"]["global_context"] == original
    empty = {"observation_focus": []}
    await app.aupdate_video_global_summary("ws", "asset", "新摘要", global_context=empty)
    assert json.loads(path.read_text())["metadata"]["global_context"] == empty
    app.aupdate_video_index.assert_awaited()
    with pytest.raises(ValueError):
        await app.aupdate_video_global_summary(
            "ws", "asset", "摘要", global_context={"observation_focus": "bad"}
        )
    assert json.loads(path.read_text())["metadata"]["global_context"] == empty


@pytest.mark.asyncio
async def test_video_split_timeout_diagnostics_survive_parser_failure(tmp_path, monkeypatch):
    class VideoSplitTimeoutError(Exception):
        def __init__(self, message, details):
            super().__init__(message)
            self.details = details

    path = tmp_path / "demo.mp4"
    path.write_bytes(b"video")
    details = {"job_id": "split", "timeout_phase": "queue", "steps": []}

    class Parser:
        def __init__(self, config):
            pass

        def parse_video_only(self, *args, **kwargs):
            raise VideoSplitTimeoutError("video_split queue_timeout", details)

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", Parser)
    monkeypatch.setattr(
        "hyperrag.task.handler.ActioVideoParserConfig", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    handler = VideoParseHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"),
        actio_config={
            "models_config_path": str(tmp_path / "models.yaml"),
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_config": str(tmp_path / "merge.yaml"),
        },
    )
    result = await handler(_build_task(path))
    assert result.status == "failed"
    assert result.data == {"video_split": details}
    assert result.error == "video_split queue_timeout"
