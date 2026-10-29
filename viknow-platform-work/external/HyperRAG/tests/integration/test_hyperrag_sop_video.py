"""
HyperRAG SOP 视频解析集成测试
"""

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sop_video_pipeline(shared_hyperrag, test_workspace: str):
    """
    SOP 视频解析端到端测试。

    测试流程：
    1. 提交 SOP 视频解析任务
    2. 等待任务完成
    3. 验证结果
    """
    hyperrag = shared_hyperrag

    # 检查必要的配置
    llm_api_key = getattr(hyperrag.config, "llm_api_key", None)
    if not llm_api_key:
        pytest.skip("LLM API key 未配置，跳过 SOP 视频测试")

    task_status_map = defaultdict(list)
    job_statuses = []

    async def on_job_status(job, prev_status, new_status):
        job_statuses.append(str(new_status))

    async def on_task_status(task, prev_status, new_status):
        task_status_map[task.task_type].append(str(new_status))

    assert hyperrag.scheduler is not None, "shared_hyperrag.scheduler 未初始化"

    old_job_cb = hyperrag.scheduler._on_job_callback
    old_task_cb = hyperrag.scheduler._on_task_callback
    hyperrag.scheduler._on_job_callback = on_job_status
    hyperrag.scheduler._on_task_callback = on_task_status

    workspace = test_workspace
    # 使用 tests/data 目录
    data_dir = Path(__file__).parent.parent / "data"
    video_path = data_dir / "video-test.mp4"
    sop_path = data_dir / "test_sop.md"

    assert video_path.exists(), "video-test.mp4 不存在"
    assert sop_path.exists(), "test_sop.md 不存在"

    try:
        job_id = await hyperrag.aadd_asset(
            workspace=workspace,
            asset_id="sop-video-test",
            asset_path=str(video_path),
            params={
                "parser_type": "sop_video",
                "sop_doc_path": str(sop_path),
            },
        )

        max_wait_time = 1800  # 30 分钟超时（SOP 视频解析较慢）
        poll_interval = 5
        elapsed = 0
        job_status = None

        while elapsed < max_wait_time:
            job_status = await hyperrag.aget_job_status(job_id)
            if job_status:
                status = job_status.get("status")
                if status == "completed":
                    break
                if status == "failed":
                    error = job_status.get("error_message", "Unknown error")
                    pytest.fail(f"SOP 视频处理失败: {error}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            pytest.fail(f"SOP 视频处理超时（{max_wait_time}秒）")

        # 验证任务状态
        assert "running" in job_statuses or "completed" in job_statuses
        parse_states = set(task_status_map.get("sop_video_parse", []))

        # SOP 视频解析任务状态验证
        assert "completed" in parse_states or "running" in parse_states, (
            f"SOP 视频解析未完成，状态: {parse_states}"
        )

        # 验证资产状态
        asset_status = await hyperrag.aget_asset_status(workspace, "sop-video-test")
        assert asset_status is not None, "资产状态未找到"
        assert asset_status.status.value in ["completed", "indexing"], (
            f"资产状态异常: {asset_status.status}"
        )

    finally:
        try:
            if hyperrag.scheduler is not None:
                hyperrag.scheduler._on_job_callback = old_job_cb
                hyperrag.scheduler._on_task_callback = old_task_cb
        except Exception:
            pass
