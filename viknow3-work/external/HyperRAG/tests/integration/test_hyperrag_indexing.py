"""
完整 HyperRAG 流程测试（索引为主）

测试目标：
1. 注册自定义 task handlers（doc_parse, chunking）
2. 使用 HyperRAGV2 + SingleLayerKGService 处理 PDF 文档
3. 验证完整的处理流程：
   - 文档解析（MinerU）
   - 图像增强（ImageEnhancer）
   - 分块（HyperChunker）
   - 索引（SingleLayerKGService）

使用真实的数据库和模型 API 进行测试
"""

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest
from hyperrag.task import TaskRegistry
from loguru import logger


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_pipeline_with_task_management(shared_hyperrag, test_workspace: str):
    """
    测试使用 task 和 SingleLayerKGService 管理文档解析的完整 HyperRAG 流程

    使用真实的：
    - Redis (任务队列)
    - Postgres (KV, Vector, DocStatus)
    - Neo4j (Graph)
    - LLM/Embedding API
    """
    logger.info("=" * 60)
    logger.info("开始测试：完整HyperRAG流程（真实服务）")
    logger.info("=" * 60)

    # 默认 handlers 已在模块初始化时注册，这里仅校验
    logger.info(f"已注册的task类型: {TaskRegistry.list_types()}")

    task_status_map = defaultdict(list)
    job_statuses = []

    async def on_job_status(job, prev_status, new_status):
        job_statuses.append(str(new_status))

    async def on_task_status(task, prev_status, new_status):
        task_status_map[task.task_type].append(str(new_status))

    # 使用共享实例，避免 RW 锁冲突
    hyperrag = shared_hyperrag
    assert hyperrag.scheduler is not None, "shared_hyperrag.scheduler 未初始化"

    # 关键：把回调绑定到共享 scheduler（提交 job 之前）
    old_job_cb = hyperrag.scheduler._on_job_callback
    old_task_cb = hyperrag.scheduler._on_task_callback
    hyperrag.scheduler._on_job_callback = on_job_status
    hyperrag.scheduler._on_task_callback = on_task_status

    try:
        logger.info(f"HyperRAG初始化完成，实例ID: {hyperrag.instance_id}")
        logger.info(f"当前模式: {hyperrag.current_mode}")

        # 使用统一的 test_workspace fixture
        workspace = test_workspace
        logger.info(f"使用workspace: {workspace}")

        # 准备测试文档
        test_pdf = Path(__file__).parent / "data" / "mini_lam.pdf"
        assert test_pdf.exists(), f"测试文档不存在: {test_pdf}"

        asset_id = "mini_lam"
        logger.info(f"准备添加文档: {test_pdf}, asset_id={asset_id}")

        # 提交文档处理任务
        job_id = await hyperrag.aadd_asset(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=str(test_pdf),
            params={"language": "zh"},
        )

        logger.info(f"文档已提交，job_id: {job_id}")

        # 等待处理完成（轮询job状态）
        logger.info("等待文档处理完成...")
        max_wait_time = 1200  # 10分钟超时（真实 API 可能需要更长时间）
        poll_interval = 2  # 更短的轮询间隔
        elapsed = 0

        while elapsed < max_wait_time:
            job_status = await hyperrag.aget_job_status(job_id)

            if job_status:
                status = job_status.get("status")
                logger.info(f"Job状态: {status}, 已等待: {elapsed}秒")
                # 打印job_status中各个task的状态
                for stage, task in zip(
                    job_status.get("stages", []), job_status.get("task_ids", [])
                ):
                    task_status = await hyperrag.scheduler.get_task_status(task)
                    logger.info(f"Stage: {stage}, Task状态: {task_status.get('status')}")
                if status == "completed":
                    logger.info("✅ 文档处理完成！")
                    break
                if status == "failed":
                    error = job_status.get("error_message", "Unknown error")
                    pytest.fail(f"文档处理失败: {error}")
                    break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            pytest.fail(f"文档处理超时（{max_wait_time}秒）")

        # 验证 MinerU 缓存生成位置
        cache_dir = Path(hyperrag.config.working_dir) / workspace / "mineru_cache" / asset_id
        assert cache_dir.exists(), f"MinerU 缓存未生成: {cache_dir}"

        status = await hyperrag.aget_asset_status(workspace, asset_id)
        assert status is not None and status.status.value == "completed"
        chunks = await hyperrag.aget_asset_chunks(workspace, asset_id)
        assert chunks, "KG chunks 为空"
        logger.info(f"✅ KG 索引完成，chunk 数量: {len(chunks)}")

        # 再次检查索引统计
        index_status = await hyperrag.aget_workspace_status(workspace)
        status_counts = index_status.get("status_counts", {})
        completed_count = status_counts.get("completed", 0)
        assert completed_count >= 1, f"期望至少1个已完成文档，实际: {completed_count}"
        logger.info(f"✅ 索引验证通过: {completed_count} 个文档已完成")

        # 校验状态回调触发
        assert "running" in job_statuses
        assert "completed" in job_statuses

        doc_states = set(task_status_map.get("doc_parse", []))
        image_states = set(task_status_map.get("image_caption", []))
        index_states = set(task_status_map.get("kg_index", []))

        assert {"queued", "running", "completed"}.issubset(doc_states)
        assert {"queued", "running", "completed"}.issubset(image_states)
        assert {"pending", "queued", "running", "completed"}.issubset(index_states)

        # 校验 image_caption 结果包含视觉要点
        job_status = await hyperrag.aget_job_status(job_id)
        image_task = None
        for tid in job_status.get("task_ids", []):
            t = await hyperrag.scheduler.get_task_status(tid)
            if t and t.get("task_type") == "image_caption":
                image_task = t
                break
        assert image_task, "找不到 image_caption 任务"
        image_content = image_task.get("result", {}).get("content", "")
        assert "[视觉要点]" in image_content

        logger.info("=" * 60)
        logger.info("✅ 所有测试通过！")
        logger.info("=" * 60)

    finally:
        # 恢复回调，避免影响其它用例
        try:
            if hyperrag.scheduler is not None:
                hyperrag.scheduler._on_job_callback = old_job_cb
                hyperrag.scheduler._on_task_callback = old_task_cb
        except Exception:
            pass

        # cleanup 由 test_workspace fixture 自动处理
        logger.info("测试结束")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_data_with_image_chunks(shared_hyperrag, test_workspace: str):
    """
    测试 query_data 返回图片 chunk 并正确填充 chunk_detail

    验证点：
    1. 使用 lam.pdf 建立索引（包含图片）
    2. 调用 aquery_data 获取结构化数据
    3. 确认返回的 chunks 中至少有一个图片 chunk
    4. 调用 aget_chunk_detail 验证能正确获取 chunk_detail（包括 image_url、locations 等）
    """
    from .conftest import _wait_job_completed

    hyperrag = shared_hyperrag
    workspace = test_workspace

    logger.info("=" * 60)
    logger.info("开始测试：query_data 图片 chunk 显示")
    logger.info("=" * 60)

    # 使用 mini_lam.pdf 避免大文件导致的超时或资源问题
    test_pdf = Path(__file__).parent / "data" / "mini_lam.pdf"
    assert test_pdf.exists(), f"测试文档不存在: {test_pdf}"

    asset_id = "mini_lam"
    logger.info(f"准备添加文档: {test_pdf}, asset_id={asset_id}")

    # 提交文档处理任务
    job_id = await hyperrag.aadd_asset(
        workspace=workspace,
        asset_id=asset_id,
        asset_path=str(test_pdf),
        params={"language": "zh"},
    )

    logger.info(f"文档已提交，job_id: {job_id}")

    # 等待索引完成
    await _wait_job_completed(hyperrag, job_id, timeout_s=1200)
    logger.info("✅ 索引完成")

    # 等待一小段时间，确保数据完全写入
    await asyncio.sleep(3)

    # 使用 query_data 获取结构化数据
    query = "这份文档讲了什么？有哪些图片或表格？"
    result = await hyperrag.aquery_data(
        workspace=workspace,
        query=query,
        mode="hybrid",
        where=None,
    )

    assert result.get("status") == "success", f"查询失败: {result}"
    data = result.get("data", {})
    chunks = data.get("chunks", [])

    chunks = await hyperrag.aget_chunks_by_ids(
        workspace, [chunk.get("chunk_id") for chunk in chunks]
    )

    logger.info(f"query_data 返回了 {len(chunks)} 个 chunks")
    assert len(chunks) > 0, "应该返回至少一个 chunk"

    # 验证：至少有一个 chunk 能获取到完整的 chunk_detail
    found_image_chunk = False
    chunk_details_checked = 0

    for chunk in chunks.values():  # 检查前10个chunk（避免过多调用）
        chunk_id = chunk.get("chunk_id")
        if not chunk_id:
            continue

        # 调用 aget_chunk_detail 获取完整 chunk 信息
        chunk_detail = await hyperrag.aget_chunk_by_id(workspace, chunk_id)
        chunk_details_checked += 1

        if not chunk_detail:
            logger.warning(f"未能获取 chunk_detail: {chunk_id}")
            continue

        # 检查 chunk_detail 的基本结构
        assert "chunk_id" in chunk_detail, "chunk_detail 应包含 chunk_id"
        assert "content" in chunk_detail, "chunk_detail 应包含 content"
        assert "metadata" in chunk_detail, "chunk_detail 应包含 metadata"

        metadata = chunk_detail.get("metadata", {})
        chunk_type = metadata.get("chunk_type")
        segment_ids = metadata.get("segment_ids", [])

        logger.info(
            f"Chunk {chunk_id}: type={chunk_type}, "
            f"segment_ids={len(segment_ids)}, "
            f"has_locations={('locations' in metadata)}, "
            f"has_image_url={('image_url' in metadata)}"
        )

        # 检查是否为图片 chunk
        if chunk_type == "image":
            found_image_chunk = True
            logger.info(f"✅ 发现图片 chunk: {chunk_id}")

            # 验证图片 chunk 的详细信息
            assert len(segment_ids) > 0, "图片 chunk 应该有 segment_ids"

            # 验证 locations 信息
            locations = metadata.get("locations", [])
            if locations:
                logger.info(f"  - locations 数量: {len(locations)}")
                first_loc = locations[0]
                logger.info(
                    "  - 第一个 location: page={page}, bbox={bbox}, page_size={page_size}".format(
                        page=first_loc.get("page"),
                        bbox=first_loc.get("bbox"),
                        page_size=first_loc.get("page_size"),
                    )
                )
                for loc in locations:
                    assert loc.get("bbox"), "location 应包含 bbox"
                    assert loc.get("page_size"), "location 应包含 page_size"

            # 验证 image_url
            image_url = metadata.get("image_url")
            if image_url:
                logger.info(f"  - image_url: {image_url}")
                assert isinstance(image_url, str) and len(image_url) > 0, (
                    "image_url 应该是非空字符串"
                )

            # 至少找到一个，可以提前退出
            break

    logger.info(f"检查了 {chunk_details_checked} 个 chunk_detail")
    assert chunk_details_checked > 0, "应该至少能获取一个 chunk_detail"

    # 如果没找到图片 chunk，记录日志但不失败（因为 query 可能没有召回图片相关的内容）
    if not found_image_chunk:
        logger.warning("⚠️  在 query_data 返回的 chunks 中未找到 chunk_type=image 的 chunk")
        logger.warning("这可能是因为查询问题召回的主要是文本 chunk")

        # 直接从 KG 读取所有 chunks，验证是否有图片 chunk 被索引。
        chunks_list = [
            chunk["chunk_id"] for chunk in await hyperrag.aget_asset_chunks(workspace, asset_id)
        ]
        if chunks_list:
            logger.info(f"文档总共有 {len(chunks_list)} 个 chunks")

            # 随机检查几个 chunk
            import random

            sample_size = min(20, len(chunks_list))
            sample_chunks = (
                random.sample(chunks_list, sample_size)
                if len(chunks_list) > sample_size
                else chunks_list
            )

            logger.info(f"随机检查 {len(sample_chunks)} 个 chunk...")
            for chunk_id in sample_chunks:
                chunk_detail = await hyperrag.aget_chunk_by_id(workspace, str(chunk_id))
                if chunk_detail and chunk_detail.get("metadata", {}).get("chunk_type") == "image":
                    found_image_chunk = True
                    logger.info(f"✅ 在文档 chunks 中找到图片 chunk: {chunk_id}")

                    metadata = chunk_detail.get("metadata", {})
                    segment_ids = metadata.get("segment_ids", [])
                    locations = metadata.get("locations", [])
                    image_url = metadata.get("image_url")

                    logger.info(f"  - segment_ids: {len(segment_ids)}")
                    logger.info(f"  - locations: {len(locations)}")
                    logger.info(f"  - image_url: {image_url}")

                    # 验证关键字段
                    assert len(segment_ids) > 0, "图片 chunk 应该有 segment_ids"
                    if locations:
                        for loc in locations:
                            assert loc.get("bbox"), "location 应包含 bbox"
                            assert loc.get("page_size"), "location 应包含 page_size"
                    break

    # 最终断言：应该在索引中存在图片 chunk
    assert found_image_chunk, (
        "索引中应该存在至少一个图片 chunk。如果失败，说明 chunk_meta 或 chunk_detail 填充存在问题。"
    )

    logger.info("=" * 60)
    logger.info("✅ query_data 图片 chunk 测试通过！")
    logger.info("=" * 60)
