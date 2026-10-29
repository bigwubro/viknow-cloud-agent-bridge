from __future__ import annotations

from hyperrag.task.job_builder import JobBuilder
from hyperrag.task.models import TaskResource, TaskType
from hyperrag.task.registry import TaskRegistry


def _noop_handler(*args, **kwargs):
    return None


def test_video_job_indexes_all_vectors_in_kg_stage() -> None:
    original_types = dict(TaskRegistry._types)
    try:
        TaskRegistry._types = {}
        for task_type in (
            TaskType.DELETE_ASSET,
            TaskType.VIDEO_PARSE,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ):
            TaskRegistry.register(task_type, _noop_handler, TaskResource())

        job, tasks = JobBuilder.from_asset(
            workspace="demo-workspace",
            asset_id="demo-video",
            asset_path="/tmp/demo.mp4",
            params={"title": "demo video"},
        )

        assert job.stages == ["delete_asset", "video_parse", "kg_index", "bm25_index"]
        assert [task.task_type for task in tasks] == [
            TaskType.DELETE_ASSET,
            TaskType.VIDEO_PARSE,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ]
    finally:
        TaskRegistry._types = original_types


def test_job_builder_embeds_request_id_in_job_and_tasks() -> None:
    original_types = dict(TaskRegistry._types)
    try:
        TaskRegistry._types = {}
        for task_type in (
            TaskType.DELETE_ASSET,
            TaskType.VIDEO_PARSE,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ):
            TaskRegistry.register(task_type, _noop_handler, TaskResource())

        request_id = "req-test-1234"
        job, tasks = JobBuilder.from_asset(
            workspace="demo-workspace",
            asset_id="demo-video",
            asset_path="/tmp/demo.mp4",
            params={"title": "demo video"},
            request_id=request_id,
        )

        assert job.metadata["_request_id"] == request_id
        assert all(task.metadata["_request_id"] == request_id for task in tasks)
    finally:
        TaskRegistry._types = original_types


def test_job_builder_uses_video_update_job_when_requested() -> None:
    original_types = dict(TaskRegistry._types)
    try:
        TaskRegistry._types = {}
        for task_type in (
            TaskType.DELETE_INDEX,
            TaskType.UPDATE_CAPTION,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ):
            TaskRegistry.register(task_type, _noop_handler, TaskResource())

        job, tasks = JobBuilder.from_asset(
            workspace="demo-workspace",
            asset_id="demo-video",
            asset_path="/tmp/demo.mp4",
            params={"job_type": "video_update", "edited_sop_full_path": "/tmp/sop-full.json"},
        )

        assert job.job_name == "video_update"
        assert job.stages == [
            "delete_index",
            "update_caption",
            "kg_index",
            "bm25_index",
        ]
        assert [task.task_type for task in tasks] == [
            TaskType.DELETE_INDEX,
            TaskType.UPDATE_CAPTION,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ]
    finally:
        TaskRegistry._types = original_types


def test_job_builder_skips_bm25_for_precompile_mode() -> None:
    original_types = dict(TaskRegistry._types)
    try:
        TaskRegistry._types = {}
        for task_type in (
            TaskType.DELETE_ASSET,
            TaskType.DOC_PARSE,
            TaskType.KG_INDEX,
            TaskType.BM25_INDEX,
        ):
            TaskRegistry.register(task_type, _noop_handler, TaskResource())

        job, tasks = JobBuilder.from_asset(
            workspace="demo-workspace",
            asset_id="demo-doc",
            asset_path="/tmp/demo.md",
            params={"pre_compile": True},
        )

        assert "bm25_index" not in job.stages
        assert [task.task_type for task in tasks] == [
            TaskType.DELETE_ASSET,
            TaskType.DOC_PARSE,
            TaskType.KG_INDEX,
        ]
    finally:
        TaskRegistry._types = original_types
