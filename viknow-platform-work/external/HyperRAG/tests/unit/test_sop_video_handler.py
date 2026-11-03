from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperrag.task.handler import SOPVideoHandler
from hyperrag.task.models import Task, TaskResource, TaskStatus


class _ArtifactStoreStub:
    def __init__(self, root: Path) -> None:
        self._root = root

    def get_video_cache_root(self, workspace: str) -> Path:
        return self._root / workspace


def _build_task(tmp_path: Path) -> Task:
    video_path = tmp_path / "demo.mp4"
    sop_doc_path = tmp_path / "demo.md"
    video_path.write_text("video", encoding="utf-8")
    sop_doc_path.write_text("sop", encoding="utf-8")
    return Task(
        task_id="task-1",
        task_type="sop_video_parse",
        workspace="ws",
        asset_id="asset-1",
        status=TaskStatus.PENDING,
        resource=TaskResource(),
        metadata={
            "asset_path": str(video_path),
            "sop_doc_path": str(sop_doc_path),
        },
    )


@pytest.mark.asyncio
async def test_sop_video_handler_writes_transcript_into_sop_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = [{"time": "0.00", "text": "开始操作"}]
    parser_configs = []
    parse_calls = []
    sop_full = {
        "metadata": {
            "sop_id": "sop-1",
            "title": "测试 SOP",
            "version": "1.0",
            "semantic_type": "sop_video",
            "status": "draft",
            "sources": [
                {
                    "source_id": "video_demo",
                    "source_type": "video",
                    "source_url": "video://demo",
                }
            ],
        },
        "overview": {},
        "document_control": [],
        "instructions": [],
        "transcript": transcript,
        "segments": [],
    }

    completeness = {
        "caption_stats": {
            "total": 3,
            "succeeded": 2,
            "failed": 1,
            "failed_nodes": [{"node_id": "failed-clip", "retry_error": "rejected"}],
        },
        "indexing_outcome": "partial_failed",
        "global_context_quality_warnings": ["观察计划可能过于泛化"],
    }
    sop_full["metadata"].update(completeness)

    class FakeActioVideoParser:
        def __init__(self, config):
            parser_configs.append(config)

        def parse(self, video_path, sop_path, *, output_path=None):
            parse_calls.append(
                {
                    "video_path": video_path,
                    "sop_path": sop_path,
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

    artifact_store = _ArtifactStoreStub(tmp_path / "cache")
    handler = SOPVideoHandler(
        artifact_store=artifact_store,
        actio_config={
            "segment_config": str(tmp_path / "segment.yaml"),
            "aggregate_config": str(tmp_path / "aggregate.yaml"),
            "merge_dtw_config": str(tmp_path / "merge.dtw.yaml"),
            "sop_schema_path": str(tmp_path / "sop_schema.md"),
            "models_config_path": str(tmp_path / "model_calls.yaml"),
            "annotate_max_concurrency": 2,
            "device": "cpu",
        },
    )
    task = _build_task(tmp_path)

    result = await handler(task)

    assert result.status == "completed"
    assert result.data is not None
    assert result.data["sop_full"]["transcript"] == transcript
    assert result.data["asset"]["extra"]["duration_sec"] == 95.5
    for key, value in completeness.items():
        assert result.data["asset"]["extra"][key] == value
    assert parse_calls == [
        {
            "video_path": Path(task.metadata["asset_path"]),
            "sop_path": Path(task.metadata["sop_doc_path"]),
            "output_path": (
                artifact_store.get_video_cache_root(task.workspace)
                / task.asset_id
                / "sop-full.json"
            ),
        }
    ]
    assert parser_configs[0].models_config == str(tmp_path / "model_calls.yaml")
    assert parser_configs[0].merge_config == str(tmp_path / "merge.dtw.yaml")
    assert parser_configs[0].device == "cpu"
    assert parser_configs[0].annotate_max_concurrency == 2

    saved = json.loads(
        (
            artifact_store.get_video_cache_root(task.workspace) / task.asset_id / "sop-full.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["transcript"] == transcript


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "models_config_path",
        "segment_config",
        "aggregate_config",
        "merge_dtw_config",
        "sop_schema_path",
    ],
)
async def test_sop_video_handler_rejects_missing_explicit_config(tmp_path, monkeypatch, missing):
    config = {
        name: str(tmp_path / name)
        for name in [
            "models_config_path",
            "segment_config",
            "aggregate_config",
            "merge_dtw_config",
            "sop_schema_path",
        ]
    }
    del config[missing]

    def unexpected_parser(*args, **kwargs):
        pytest.fail("Parser must not run with missing configuration")

    monkeypatch.setattr("hyperrag.task.handler.ActioVideoParser", unexpected_parser)
    handler = SOPVideoHandler(
        artifact_store=_ArtifactStoreStub(tmp_path / "cache"), actio_config=config
    )
    result = await handler(_build_task(tmp_path))
    assert result.status == "failed"
    assert missing in result.error
