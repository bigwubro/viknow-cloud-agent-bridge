"""
HyperRAG 文档删除集成测试

目标：
1) 通过 Job/Task 调度执行删除
2) 并发提交 2 个删除请求，验证能被 scheduler 排队执行并最终都成功
3) 使用真实文档（完整索引流程），确保删除功能能够清理所有相关数据
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from loguru import logger

from hyperrag.asset_status import AssetStatus
from hyperrag.hyperrag_v2 import HyperRAGV2

from .conftest import _wait_job_completed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_two_assets_concurrently(shared_hyperrag: HyperRAGV2, test_workspace: str):
    """
    并发提交两个删除 job，验证：
    - 两个文档都能成功完成完整索引流程
    - 两个删除 job 都能完成
    - 两个文档最终都被删除（包括知识图谱、向量索引、chunk_meta 等）
    - asset_status 变为 deleted
    """
    v2 = shared_hyperrag
    workspace = test_workspace
    await v2.initialize()
    await v2._get_kg_experiment(workspace)

    # 准备两份测试文档（使用同一个文件，但用不同的 asset_id）
    test_pdf_1 = Path(__file__).parent / "data" / "mini_lam.pdf"
    assert test_pdf_1.exists(), f"测试文档不存在: {test_pdf_1}"
    test_pdf_2 = Path(__file__).parent / "data" / "lam.pdf"
    assert test_pdf_2.exists(), f"测试文档不存在: {test_pdf_2}"

    asset_id_1 = "mini_lam"
    asset_id_2 = "lam"

    logger.info("=" * 60)
    logger.info("步骤1: 添加两个文档并等待索引完成")
    logger.info("=" * 60)

    # 提交两个文档处理任务
    index_job_id_1 = await v2.aadd_asset(
        workspace=workspace,
        asset_id=asset_id_1,
        asset_path=str(test_pdf_1),
        params={"language": "zh"},
    )
    logger.info(f"文档1已提交，job_id: {index_job_id_1}")

    index_job_id_2 = await v2.aadd_asset(
        workspace=workspace,
        asset_id=asset_id_2,
        asset_path=str(test_pdf_2),
        params={"language": "zh"},
    )
    logger.info(f"文档2已提交，job_id: {index_job_id_2}")

    # 等待两个文档索引完成
    await asyncio.gather(
        _wait_job_completed(v2, index_job_id_1, timeout_s=1200),
        _wait_job_completed(v2, index_job_id_2, timeout_s=1200),
    )
    logger.info("✅ 两个文档索引完成")

    # 验证文档已成功索引。
    chunks1 = await v2.aget_asset_chunks(workspace, asset_id_1)
    chunks2 = await v2.aget_asset_chunks(workspace, asset_id_2)
    logger.info(f"文档1有 {len(chunks1)} 个 chunks，文档2有 {len(chunks2)} 个 chunks")
    assert len(chunks1) > 0 and len(chunks2) > 0, "文档应该包含 chunks"

    logger.info("=" * 60)
    logger.info("步骤2: 并发提交两个删除请求")
    logger.info("=" * 60)

    # 并发提交两个删除请求（关键：同时删除 2 个文档）
    delete_job_id_1, delete_job_id_2 = await asyncio.gather(
        v2.adelete_asset(workspace, asset_id_1),
        v2.adelete_asset(workspace, asset_id_2),
    )
    assert delete_job_id_1 and delete_job_id_2 and delete_job_id_1 != delete_job_id_2
    logger.info(f"删除任务已提交，job_ids: {delete_job_id_1}, {delete_job_id_2}")

    # 等待两个删除任务完成
    await asyncio.gather(
        _wait_job_completed(v2, delete_job_id_1, timeout_s=600),
        _wait_job_completed(v2, delete_job_id_2, timeout_s=600),
    )
    logger.info("✅ 两个删除任务完成")

    logger.info("=" * 60)
    logger.info("步骤3: 验证删除结果")
    logger.info("=" * 60)

    assert not await v2.aget_asset_chunks(workspace, asset_id_1)
    assert not await v2.aget_asset_chunks(workspace, asset_id_2)
    logger.info("✅ KG 存储验证通过")

    # 验证 asset_status 变为 deleted
    st1 = await v2.aget_asset_status(workspace, asset_id_1)
    st2 = await v2.aget_asset_status(workspace, asset_id_2)
    assert st1 is not None and st1.status == AssetStatus.DELETED, (
        f"文档1状态应该是 DELETED，实际: {st1.status if st1 else None}"
    )
    assert st2 is not None and st2.status == AssetStatus.DELETED, (
        f"文档2状态应该是 DELETED，实际: {st2.status if st2 else None}"
    )
    logger.info("✅ AssetStatus 验证通过")

    # 验证本地缓存已清理
    cache_dir_1 = Path(v2.config.working_dir) / workspace / "mineru_cache" / asset_id_1
    cache_dir_2 = Path(v2.config.working_dir) / workspace / "mineru_cache" / asset_id_2
    assert not cache_dir_1.exists(), f"文档1的本地缓存应该被清理: {cache_dir_1}"
    assert not cache_dir_2.exists(), f"文档2的本地缓存应该被清理: {cache_dir_2}"
    logger.info("✅ 本地缓存清理验证通过")

    logger.info("=" * 60)
    logger.info("✅ 所有测试通过！")
    logger.info("=" * 60)
