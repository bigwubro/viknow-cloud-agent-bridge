"""
HyperRAG 任务调度相关集成测试
"""

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_video_pipeline_with_vlm(shared_hyperrag, test_workspace: str):
    """
    使用 vLLM 部署的 Qwen3-VL 进行视频解析与 caption，验证任务回调。
    """
    task_status_map = defaultdict(list)
    job_statuses = []

    async def on_job_status(job, prev_status, new_status):
        job_statuses.append(str(new_status))

    async def on_task_status(task, prev_status, new_status):
        task_status_map[task.task_type].append(str(new_status))

    pytest.importorskip("minio")
    hyperrag = shared_hyperrag
    assert hyperrag.scheduler is not None, "shared_hyperrag.scheduler 未初始化"

    old_job_cb = hyperrag.scheduler._on_job_callback
    old_task_cb = hyperrag.scheduler._on_task_callback
    hyperrag.scheduler._on_job_callback = on_job_status
    hyperrag.scheduler._on_task_callback = on_task_status

    workspace = test_workspace
    video_path = Path(__file__).parent / "data" / "video-test.mp4"
    assert video_path.exists(), "video-test.mp4 不存在"

    try:
        job_id = await hyperrag.aadd_asset(
            workspace=workspace,
            asset_id="video-test",
            asset_path=str(video_path),
            params={"language": "zh"},
        )

        max_wait_time = 900
        poll_interval = 2
        elapsed = 0
        while elapsed < max_wait_time:
            job_status = await hyperrag.aget_job_status(job_id)
            if job_status:
                status = job_status.get("status")
                if status == "completed":
                    break
                if status == "failed":
                    error = job_status.get("error_message", "Unknown error")
                    pytest.fail(f"视频处理失败: {error}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            pytest.fail(f"视频处理超时（{max_wait_time}秒）")

        assert "running" in job_statuses and "completed" in job_statuses
        parse_states = set(task_status_map.get("video_parse", []))
        index_states = set(task_status_map.get("kg_index", []))
        assert {"queued", "running", "completed"}.issubset(parse_states)
        assert {"pending", "queued", "running", "completed"}.issubset(index_states)
        assert "video_embedding" not in task_status_map

    finally:
        try:
            if hyperrag.scheduler is not None:
                hyperrag.scheduler._on_job_callback = old_job_cb
                hyperrag.scheduler._on_task_callback = old_task_cb
        except Exception:
            pass

        # workspace 清理由 test_workspace fixture 自动处理
