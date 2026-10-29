"""
HyperRAG 集成测试 - 单 workspace 模式
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hyperrag import HyperRAGConfig, HyperRAGV2
from hyperrag.asset_status import AssetProcessingStatus, AssetStatus
from hyperrag.task.models import JobInstance, Task, TaskResource, TaskStatus, TaskType


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.TaskScheduler")
@patch("hyperrag.hyperrag_v2.DistributedTaskQueue")
async def test_hyperrag_single_workspace_initialization(mock_queue_cls, mock_scheduler_cls):
    """测试单 workspace 模式初始化"""
    mock_queue = AsyncMock()
    mock_queue.initialize = AsyncMock()
    mock_queue.redis = AsyncMock()
    mock_queue_cls.return_value = mock_queue

    mock_scheduler = AsyncMock()
    mock_scheduler.start_heartbeat = AsyncMock()
    mock_scheduler.switch_to_rw = AsyncMock()
    mock_scheduler_cls.return_value = mock_scheduler

    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
    )

    hyperrag = HyperRAGV2(config)

    # 初始化应该成功
    knowledgebase_storage = AsyncMock()
    knowledgebase_storage.initialize = AsyncMock()
    knowledgebase_storage.get_all = AsyncMock(return_value=[])
    discovery_storage = AsyncMock()
    discovery_storage.initialize = AsyncMock()
    with (
        patch(
            "hyperrag.hyperrag_v2.get_knowledgebase_info_storage",
            return_value=knowledgebase_storage,
        ),
        patch(
            "hyperrag.hyperrag_v2.get_knowledge_discovery_catalog_storage",
            return_value=discovery_storage,
        ),
    ):
        await hyperrag.initialize()
        await hyperrag._knowledge_discovery_backfill_task

    assert hyperrag.scheduler is not None
    assert hyperrag.config.workspace == "test-workspace"


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.JobBuilder.from_asset")
@patch("hyperrag.hyperrag_v2.get_asset_status_storage")
async def test_hyperrag_add_asset(mock_get_status_storage, mock_from_asset):
    """测试添加资产"""
    mock_storage = AsyncMock()
    mock_storage.set_status = AsyncMock()
    mock_storage.get_status = AsyncMock(return_value=None)
    mock_get_status_storage.return_value = mock_storage
    captured: dict[str, object] = {}

    def _fake_from_asset(**kwargs):
        captured["params"] = kwargs.get("params")
        return Mock(), []

    mock_from_asset.side_effect = _fake_from_asset

    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
    )

    hyperrag = HyperRAGV2(config)
    hyperrag._initialized = True
    hyperrag.scheduler = AsyncMock()
    hyperrag.scheduler.submit_job = AsyncMock(return_value="job-1")
    hyperrag._queue = Mock()
    hyperrag._queue.redis = AsyncMock()

    # 创建测试文件
    test_file = Path("./test.pdf")
    test_file.write_text("test content")

    try:
        # 添加资产
        job_id = await hyperrag.aadd_asset(
            workspace="test-workspace",
            asset_id="test-doc-1",
            asset_path=str(test_file),
            params={"language": "zh"},
            fast_ingest=True,
        )
        assert job_id == "job-1"
        assert captured["params"]["fast_ingest"] is True
    finally:
        # 清理
        if test_file.exists():
            test_file.unlink()


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.JobBuilder.from_asset")
@patch("hyperrag.hyperrag_v2.get_asset_status_storage")
async def test_precompile_retry_only_submits_failed_er_chunks(
    mock_get_status_storage, mock_from_asset, tmp_path: Path
):
    mock_storage = AsyncMock()
    mock_storage.set_status = AsyncMock()
    mock_storage.get_status = AsyncMock(
        return_value=AssetProcessingStatus(
            asset_id="test-doc-1",
            status=AssetStatus.COMPLETED,
            created_at=1.0,
            updated_at=2.0,
            metadata={
                "precompile_failed_er_chunks": [
                    {"chunk_id": "chunk-a", "error": "missing description"},
                    {"chunk_id": "chunk-b", "error": "missing description"},
                ]
            },
        )
    )
    mock_get_status_storage.return_value = mock_storage
    captured: dict[str, object] = {}

    def _fake_from_asset(**kwargs):
        captured["params"] = kwargs["params"]
        return Mock(), []

    mock_from_asset.side_effect = _fake_from_asset
    hyperrag = HyperRAGV2(HyperRAGConfig(working_dir="./test-storage", workspace="test"))
    hyperrag._initialized = True
    hyperrag.scheduler = AsyncMock()
    hyperrag.scheduler.submit_job = AsyncMock(return_value="job-1")
    hyperrag._queue = Mock()
    hyperrag._queue.redis = AsyncMock()
    hyperrag.adelete_knowledgebase_info = AsyncMock()  # type: ignore[method-assign]

    asset_path = tmp_path / "test.pdf"
    asset_path.write_text("test content")
    await hyperrag.aadd_asset(
        workspace="test",
        asset_id="test-doc-1",
        asset_path=str(asset_path),
        pre_compile=True,
    )

    assert captured["params"]["retry_er_chunk_ids"] == ["chunk-a", "chunk-b"]
    hyperrag.adelete_knowledgebase_info.assert_awaited_once_with("PRECOMPILED")  # type: ignore[attr-defined]


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.get_asset_status_storage")
async def test_hyperrag_adelete_asset_reuses_inflight_delete_job(
    mock_get_status_storage,
):
    """重复删除同一 asset 时应复用已有删除 job。"""
    mock_storage = AsyncMock()
    mock_storage.get_status = AsyncMock(
        return_value=AssetProcessingStatus(
            asset_id="test-doc-1",
            status=AssetStatus.DELETING,
            created_at=1.0,
            updated_at=2.0,
            parse_mode="video",
        )
    )
    mock_get_status_storage.return_value = mock_storage

    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
    )

    hyperrag = HyperRAGV2(config)
    hyperrag._initialized = True
    hyperrag.scheduler = AsyncMock()
    hyperrag._queue = Mock()
    hyperrag._queue.redis = AsyncMock()
    hyperrag._queue.redis.get = AsyncMock(return_value="job-delete-1")
    hyperrag._queue.get_job = AsyncMock(
        return_value=JobInstance(
            job_id="job-delete-1",
            job_name="delete",
            workspace="test-workspace",
            asset_id="test-doc-1",
            asset_path="",
            stages=["delete_asset"],
            status="running",
        )
    )

    job_id = await hyperrag.adelete_asset("test-workspace", "test-doc-1")

    assert job_id == "job-delete-1"
    hyperrag.scheduler.submit_job.assert_not_called()
    mock_storage.set_status.assert_not_called()


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.get_asset_status_storage")
async def test_on_job_callback_updates_summary_after_completed_status(
    mock_get_status_storage,
):
    mock_storage = AsyncMock()
    mock_storage.set_status = AsyncMock()
    mock_get_status_storage.return_value = mock_storage

    config = HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace")
    hyperrag = HyperRAGV2(config)
    hyperrag._queue = Mock()
    hyperrag._queue.get_task = AsyncMock(
        return_value=Task(
            task_id="kg-task-1",
            task_type=TaskType.KG_INDEX,
            workspace="test-workspace",
            asset_id="asset-1",
            status=TaskStatus.COMPLETED,
            resource=TaskResource(),
            result={
                "document_summary": "文档摘要",
                "asset": {"title": "资产标题"},
            },
        )
    )
    hyperrag.async_knowledgebase_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(summarization="知识库摘要")
    )

    job = JobInstance(
        job_id="job-1",
        job_name="document",
        workspace="test-workspace",
        asset_id="asset-1",
        asset_path="/tmp/a.pdf",
        stages=["kg_index"],
        task_ids=["kg-task-1"],
        status="completed",
    )

    await hyperrag._on_job_callback(job, "running", "completed")

    mock_storage.set_status.assert_awaited_once_with(
        "test-workspace",
        "asset-1",
        AssetStatus.COMPLETED,
        progress=1.0,
    )
    hyperrag.async_knowledgebase_summary.assert_awaited_once_with(  # type: ignore[attr-defined]
        "test-workspace",
        asset_id="asset-1",
        asset_title="资产标题",
        document_summary="文档摘要",
    )


@pytest.mark.asyncio
@patch("hyperrag.hyperrag_v2.get_asset_status_storage")
async def test_precompile_job_does_not_create_knowledgebase_summary(mock_get_status_storage):
    mock_storage = AsyncMock()
    mock_storage.set_status = AsyncMock()
    mock_get_status_storage.return_value = mock_storage

    hyperrag = HyperRAGV2(HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace"))
    hyperrag.async_knowledgebase_summary = AsyncMock()  # type: ignore[method-assign]
    job = JobInstance(
        job_id="job-1",
        job_name="document",
        workspace="PRECOMPILED",
        asset_id="asset-1",
        asset_path="/tmp/a.pdf",
        stages=["kg_index"],
        task_ids=[],
        status="completed",
        metadata={"pre_compile": True},
    )

    await hyperrag._on_job_callback(job, "running", "completed")

    mock_storage.set_status.assert_awaited_once_with(
        "PRECOMPILED",
        "asset-1",
        AssetStatus.COMPLETED,
        progress=1.0,
    )
    hyperrag.async_knowledgebase_summary.assert_not_awaited()  # type: ignore[attr-defined]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
