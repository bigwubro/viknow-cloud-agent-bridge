from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.task.models import Task, TaskResource, TaskResult, TaskStatus
from hyperrag.task.scheduler import TaskScheduler


def task():
    return Task(
        task_id="t",
        task_type="video_parse",
        workspace="ws",
        asset_id="asset",
        status=TaskStatus.RUNNING,
        resource=TaskResource(),
        metadata={},
    )


@pytest.mark.asyncio
async def test_failed_result_diagnostics_persist_before_status_callback():
    t = task()
    details = {"timeout_phase": "queue", "job_id": "split", "steps": []}
    queue = SimpleNamespace(update_task=AsyncMock())
    scheduler = SimpleNamespace(
        _queue=queue,
        _emit_task_status=AsyncMock(),
        _execute_task_callback=AsyncMock(),
        _fail_job=AsyncMock(),
    )
    await TaskScheduler._on_task_failed(
        scheduler,
        t,
        TaskResult(status="failed", error="queue_timeout", data={"video_split": details}),
    )
    assert t.result == {"video_split": details}
    queue.update_task.assert_awaited_once_with(t)
    scheduler._emit_task_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_metadata_then_new_success_removes_previous_error(monkeypatch):
    import hyperrag.hyperrag_v2 as module

    metadata = {}

    async def update(workspace, asset, data, remove_keys=None):
        for key in remove_keys or []:
            metadata.pop(key, None)
        metadata.update(data)

    async def set_status(workspace, asset, status, **kwargs):
        if kwargs.get("metadata"):
            metadata.update(kwargs["metadata"])

    storage = SimpleNamespace(
        update_metadata=AsyncMock(side_effect=update),
        set_status=AsyncMock(side_effect=set_status),
    )
    monkeypatch.setattr(module, "get_asset_status_storage", lambda: storage)
    rag = object.__new__(HyperRAGV2)
    rag._user_on_task_callback = None
    rag._persist_task_asset_metadata = AsyncMock()
    t = task()
    t.error_message = "queue_timeout"
    t.result = {"video_split": {"timeout_phase": "queue"}}
    await rag._on_task_callback(t, "running", "failed")
    assert metadata["video_split"]["timeout_phase"] == "queue"
    t.result = {}
    t.error_message = None
    await rag._on_task_callback(t, "queued", "running")
    assert "video_split" not in metadata
    assert storage.set_status.call_args.kwargs["error_message"] == ""
    metadata["video_split"] = {"timeout_phase": "queue"}
    await rag._on_task_callback(t, "running", "completed")
    assert "video_split" not in metadata


@pytest.mark.asyncio
async def test_first_failure_creates_status_with_timeout_metadata(monkeypatch):
    import hyperrag.hyperrag_v2 as module

    rows = {}

    async def set_status(workspace, asset, status, **kwargs):
        rows[(workspace, asset)] = {"status": status, **kwargs}

    storage = SimpleNamespace(
        set_status=AsyncMock(side_effect=set_status), update_metadata=AsyncMock()
    )
    monkeypatch.setattr(module, "get_asset_status_storage", lambda: storage)
    rag = object.__new__(HyperRAGV2)
    rag._user_on_task_callback = None
    t = task()
    t.error_message = "queue_timeout"
    t.result = {"video_split": {"timeout_phase": "queue", "job_id": "remote"}}
    await rag._on_task_callback(t, "running", "failed")
    assert rows[("ws", "asset")]["metadata"] == t.result
    storage.update_metadata.assert_not_awaited()
