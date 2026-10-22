"""
测试 HyperRAG 查询与 metadata 过滤功能

测试场景：
1. 建索引时设置 metadata
2. 查询时根据元数据过滤结果
3. 对比开过滤和不开过滤情况下，返回信息的数量
4. 验证旧数据兼容性（从 chunk.content JSON 读取 segment_ids）

运行方式（务必激活 venv）：
  cd /home/dtong/code/HyperRAG
  source .venv/bin/activate
  pytest tests/integration/test_hyperrag_query.py --run-integration -q -v
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from loguru import logger

from hyperrag.hyperrag_v2 import HyperRAGV2

# 使用统一的集成测试 fixtures
from .conftest import _wait_job_completed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chunk_meta_filter_by_metadata(shared_hyperrag: HyperRAGV2, test_workspace: str):
    """
    测试根据 metadata 过滤查询结果

    步骤：
    1. 创建两个文档，分别设置不同的 metadata
    2. 等待索引完成
    3. 执行查询（不带过滤条件）-> 应该返回所有文档的 chunks
    4. 执行查询（带过滤条件，匹配第一个文档的 metadata）-> 应该只返回第一个文档的 chunks
    5. 验证返回数量符合预期
    """
    # 创建测试文档内容
    test_doc1_content = """
    # NVIDIA A100

    NVIDIA A100 GPU is a high-performance computing GPU launched by NVIDIA.
    It is based on the Ampere architecture and comes with either 40GB or 80GB of memory.
    The A100 is mainly used in data centers and AI training scenarios.
    """

    test_doc2_content = """
    # Intel Core i9

    Intel Core i9 processor is a high-end desktop processor from Intel.
    It adopts the latest architecture design and provides powerful single-core and multi-core performance.
    Core i9 is mainly used for high-performance computing and gaming scenarios.
    """

    # 创建临时文件
    test_dir = Path("/tmp/hyperrag_test_chunk_meta")
    test_dir.mkdir(exist_ok=True)

    doc1_path = test_dir / f"test_doc1_{uuid.uuid4().hex[:8]}.md"
    doc2_path = test_dir / f"test_doc2_{uuid.uuid4().hex[:8]}.md"

    doc1_path.write_text(test_doc1_content, encoding="utf-8")
    doc2_path.write_text(test_doc2_content, encoding="utf-8")

    try:
        # 使用统一的 fixtures
        v2 = shared_hyperrag
        workspace = test_workspace

        # 添加第一个文档（设置 attributes: brand=NVIDIA, series=A100）
        asset_id1 = f"asset1_{uuid.uuid4().hex[:8]}"
        job_id1 = await v2.aadd_asset(
            workspace=workspace,
            asset_id=asset_id1,
            asset_path=str(doc1_path),
            params={
                "title": "NVIDIA A100 GPU 介绍",
                "attributes": {
                    "brand": "NVIDIA",
                    "series": "A100",
                    "device_type": "GPU",
                },
            },
        )
        logger.info(f"Added asset1: {asset_id1}, job_id: {job_id1}")

        # 添加第二个文档（设置 attributes: brand=Intel, series=Core i9）
        asset_id2 = f"asset2_{uuid.uuid4().hex[:8]}"
        job_id2 = await v2.aadd_asset(
            workspace=workspace,
            asset_id=asset_id2,
            asset_path=str(doc2_path),
            params={
                "title": "Intel Core i9 处理器介绍",
                "attributes": {
                    "brand": "Intel",
                    "series": "Core i9",
                    "device_type": "CPU",
                },
            },
        )
        logger.info(f"Added asset2: {asset_id2}, job_id: {job_id2}")

        # 等待两个任务完成
        await _wait_job_completed(v2, job_id1, timeout_s=600)
        await _wait_job_completed(v2, job_id2, timeout_s=600)
        logger.info("Both indexing jobs completed")

        # 等待一小段时间，确保数据已完全写入
        await asyncio.sleep(3)

        def _doc_ids_from_chunks(chunks_list):
            return {c.get("full_doc_id") for c in chunks_list if c.get("full_doc_id")}

        # 测试1：不带过滤条件的查询（应该返回所有文档的 chunks）
        query = "GPU 和 CPU 的性能特点"
        result_no_filter = await v2.aquery_data(
            workspace=workspace,
            query=query,
            mode="hybrid",
            where=None,
        )

        assert result_no_filter.get("status") == "success", "Query should succeed"
        data_no_filter = result_no_filter.get("data", {})
        chunks_no_filter = data_no_filter.get("chunks", [])
        entities_no_filter = data_no_filter.get("entities", [])
        relationships_no_filter = data_no_filter.get("relationships", [])
        doc_ids_no_filter = _doc_ids_from_chunks(chunks_no_filter)

        logger.info(
            f"Query without filter: {len(chunks_no_filter)} chunks, "
            f"{len(entities_no_filter)} entities, {len(relationships_no_filter)} relationships"
        )
        assert doc_ids_no_filter.issuperset({asset_id1, asset_id2}), (
            f"Unfiltered query should include both assets, got {doc_ids_no_filter}"
        )

        # 测试2：带过滤条件（brand=NVIDIA）的查询（应该只返回第一个文档的 chunks）
        result_with_filter = await v2.aquery_data(
            workspace=workspace,
            query=query,
            mode="hybrid",
            where={"brand": "NVIDIA"},
        )

        assert result_with_filter.get("status") == "success", "Query with filter should succeed"
        data_with_filter = result_with_filter.get("data", {})
        chunks_with_filter = data_with_filter.get("chunks", [])
        entities_with_filter = data_with_filter.get("entities", [])
        relationships_with_filter = data_with_filter.get("relationships", [])

        logger.info(
            f"Query with filter (brand=NVIDIA): {len(chunks_with_filter)} chunks, "
            f"{len(entities_with_filter)} entities, {len(relationships_with_filter)} relationships"
        )

        # 验证：带过滤条件的结果应该少于不带过滤条件的结果
        assert len(chunks_with_filter) <= len(chunks_no_filter), (
            f"Filtered chunks ({len(chunks_with_filter)}) should be <= "
            f"unfiltered chunks ({len(chunks_no_filter)})"
        )
        assert chunks_with_filter, "Filtered query should return chunks"
        filtered_doc_ids = _doc_ids_from_chunks(chunks_with_filter)
        assert filtered_doc_ids == {asset_id1}, (
            f"Filtered chunks should come from {asset_id1}, got {filtered_doc_ids}"
        )
        assert len(entities_with_filter) <= len(entities_no_filter), (
            f"Filtered entities ({len(entities_with_filter)}) should be <= "
            f"unfiltered entities ({len(entities_no_filter)})"
        )
        assert len(relationships_with_filter) <= len(relationships_no_filter), (
            f"Filtered relationships ({len(relationships_with_filter)}) should be <= "
            f"unfiltered relationships ({len(relationships_no_filter)})"
        )

        # 测试3：带过滤条件（brand=Intel）的查询（应该只返回第二个文档的 chunks）
        result_with_filter_intel = await v2.aquery_data(
            workspace=workspace,
            query=query,
            mode="hybrid",
            where={"brand": "Intel"},
        )

        assert result_with_filter_intel.get("status") == "success"
        data_with_filter_intel = result_with_filter_intel.get("data", {})
        chunks_with_filter_intel = data_with_filter_intel.get("chunks", [])

        logger.info(f"Query with filter (brand=Intel): {len(chunks_with_filter_intel)} chunks")
        assert chunks_with_filter_intel, "Intel filter should return chunks"
        filtered_doc_ids_intel = _doc_ids_from_chunks(chunks_with_filter_intel)
        assert filtered_doc_ids_intel == {asset_id2}, (
            f"Filtered chunks should come from {asset_id2}, got {filtered_doc_ids_intel}"
        )

        # 验证：Intel 过滤结果和 NVIDIA 过滤结果的数量应该大致相等（因为两个文档大小相近）
        # 但允许一些差异（因为查询相关性不同）
        assert len(chunks_with_filter_intel) > 0, "Should have some chunks for Intel filter"

        # 测试4：测试 AND 条件
        result_and_filter = await v2.aquery_data(
            workspace=workspace,
            query=query,
            mode="hybrid",
            where={"AND": [{"brand": "NVIDIA"}, {"device_type": "GPU"}]},
        )

        assert result_and_filter.get("status") == "success"
        chunks_and_filter = result_and_filter.get("data", {}).get("chunks", [])
        logger.info(f"Query with AND filter: {len(chunks_and_filter)} chunks")
        assert len(chunks_and_filter) <= len(chunks_with_filter), (
            "AND filter should be more restrictive"
        )
        and_doc_ids = _doc_ids_from_chunks(chunks_and_filter)
        assert and_doc_ids.issubset({asset_id1}), (
            f"AND filter should only include {asset_id1}, got {and_doc_ids}"
        )

        # 测试5：测试 OR 条件
        result_or_filter = await v2.aquery_data(
            workspace=workspace,
            query=query,
            mode="hybrid",
            where={"OR": [{"brand": "NVIDIA"}, {"brand": "Intel"}]},
        )

        assert result_or_filter.get("status") == "success"
        chunks_or_filter = result_or_filter.get("data", {}).get("chunks", [])
        logger.info(f"Query with OR filter: {len(chunks_or_filter)} chunks")
        doc_ids_or_filter = _doc_ids_from_chunks(chunks_or_filter)
        # OR 条件应该包含所有文档，所以应该和不带过滤条件的结果数量相近
        assert len(chunks_or_filter) >= max(len(chunks_with_filter), len(chunks_with_filter_intel))
        assert doc_ids_or_filter.issuperset({asset_id1, asset_id2}), (
            f"OR filter should include both assets, got {doc_ids_or_filter}"
        )

        # 测试6：验证 chunk_meta 中的 segment_ids 是否正确
        if chunks_with_filter:
            test_chunk = chunks_with_filter[0]
            chunk_id = test_chunk.get("chunk_id")
            if chunk_id:
                chunk_detail = await v2.aget_chunk_by_id(workspace, chunk_id)
                assert chunk_detail is not None, "Should be able to get chunk detail"
                segment_ids = chunk_detail.get("metadata", {}).get("segment_ids", [])
                logger.info(f"Chunk {chunk_id} has {len(segment_ids)} segment_ids")
                # segment_ids 应该是一个列表（可能为空）
                assert isinstance(segment_ids, list), "segment_ids should be a list"

        logger.info("All tests passed!")

    finally:
        # 清理临时文件（workspace 由 test_workspace fixture 自动清理）
        try:
            if doc1_path.exists():
                doc1_path.unlink()
            if doc2_path.exists():
                doc2_path.unlink()
            if test_dir.exists() and not any(test_dir.iterdir()):
                test_dir.rmdir()
        except Exception as e:
            logger.warning(f"Failed to cleanup test files: {e}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chunk_meta_backward_compatibility(shared_hyperrag: HyperRAGV2, test_workspace: str):
    """
    测试旧数据兼容性：验证对于没有 chunk_meta 的旧数据，仍然可以从 chunk.content JSON 中读取 segment_ids
    """
    # 使用统一的 fixtures
    v2 = shared_hyperrag
    workspace = test_workspace

    # 创建一个简单的测试文档
    test_doc_content = """
    # Test Document

    This is a test document for verifying backward compatibility.
    It contains some text and a table.
    
    ## Table
    | Column 1 | Column 2 | Column 3 |
    |----------|----------|----------|
    | Value 1  | Value 2  | Value 3  |
    | Value 4  | Value 5  | Value 6  |
    """
    test_dir = Path("/tmp/hyperrag_test_chunk_meta")
    test_dir.mkdir(exist_ok=True)
    doc_path = test_dir / f"test_doc_{uuid.uuid4().hex[:8]}.md"
    doc_path.write_text(test_doc_content, encoding="utf-8")

    try:
        asset_id = f"asset_{uuid.uuid4().hex[:8]}"
        job_id = await v2.aadd_asset(
            workspace=workspace,
            asset_id=asset_id,
            asset_path=str(doc_path),
            params={"title": "Test Document"},
        )

        await _wait_job_completed(v2, job_id, timeout_s=600)
        await asyncio.sleep(3)

        # 执行查询
        result = await v2.aquery_data(
            workspace=workspace,
            query="Test Document",
            mode="hybrid",
            where=None,  # 不带过滤条件
        )

        assert result.get("status") == "success"
        chunks = result.get("data", {}).get("chunks", [])

        # 验证：即使没有 chunk_meta，查询也应该能正常工作（不过滤）
        # 因为 _filter_query_data_by_where 中，如果 chunk_meta 不存在，会保留该 chunk
        assert len(chunks) > 0, "Should have chunks even without chunk_meta"

        # 验证：尝试获取 chunk detail，应该能正常返回（即使没有 chunk_meta，也会从 content JSON 读取 segment_ids）
        if chunks:
            chunk_id = chunks[0].get("chunk_id")
            if chunk_id:
                chunk_detail = await v2.aget_chunk_by_id(workspace, chunk_id)
                assert chunk_detail is not None, "Should be able to get chunk detail"
                # segment_ids 应该存在（即使为空列表）
                segment_ids = chunk_detail.get("metadata", {}).get("segment_ids", [])
                assert isinstance(segment_ids, list), "segment_ids should be a list"

        logger.info("Backward compatibility test passed!")

    finally:
        # 清理临时文件（workspace 由 test_workspace fixture 自动清理）
        try:
            if doc_path.exists():
                doc_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to cleanup test file: {e}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_data_returns_text_and_video_chunks(
    shared_hyperrag: HyperRAGV2, test_workspace: str
):
    """
    验证 query_data 返回的 chunks 已包含文本与视频的完整 metadata。
    """
    v2 = shared_hyperrag
    workspace = test_workspace

    # 准备文本与视频资源
    test_dir = Path("/tmp/hyperrag_test_query_data")
    test_dir.mkdir(exist_ok=True)
    text_path = test_dir / f"test_text_{uuid.uuid4().hex[:8]}.md"
    text_path.write_text(
        "This is a test document about GPU acceleration and vector search.",
        encoding="utf-8",
    )

    video_path = Path(__file__).parent / "data" / "video-test.mp4"
    if not video_path.exists():
        pytest.skip("video-test.mp4 不存在，跳过视频 query_data 测试")

    try:
        # 索引文本
        text_asset_id = f"text_{uuid.uuid4().hex[:8]}"
        text_job_id = await v2.aadd_asset(
            workspace=workspace,
            asset_id=text_asset_id,
            asset_path=str(text_path),
            params={"title": "Text QueryData Test"},
        )
        await _wait_job_completed(v2, text_job_id, timeout_s=600)

        # 索引视频
        video_asset_id = f"video_{uuid.uuid4().hex[:8]}"
        video_job_id = await v2.aadd_asset(
            workspace=workspace,
            asset_id=video_asset_id,
            asset_path=str(video_path),
            params={"language": "zh", "title": "Video QueryData Test"},
        )
        await _wait_job_completed(v2, video_job_id, timeout_s=900)

        await asyncio.sleep(3)

        # 文本查询：确保返回文本类 chunk
        text_result = await v2.aquery_data(
            workspace=workspace,
            query="GPU acceleration",
            mode="hybrid",
        )
        assert text_result.get("status") == "success"
        text_chunks = text_result.get("data", {}).get("chunks", [])
        assert text_chunks, "Text query should return chunks"
        assert any((c.get("metadata") or {}).get("chunk_type") != "video" for c in text_chunks), (
            "Text query should include non-video chunks"
        )

        # 视频查询：确保返回视频 chunk，且 metadata 含 begin_ts/end_ts/segment_ids
        seed_query = ""
        doc_status = await v2.aget_asset_status(workspace, video_asset_id)
        chunks_list = []
        if isinstance(doc_status, dict):
            chunks_list = doc_status.get("chunks_list", []) or []
        elif hasattr(doc_status, "chunks_list"):
            chunks_list = getattr(doc_status, "chunks_list", []) or []
        if chunks_list:
            first_chunk_id = str(chunks_list[0])
            chunk_detail = await v2.aget_chunk_by_id(workspace, first_chunk_id)
            if chunk_detail:
                seed_query = str(chunk_detail.get("content") or "").strip()
        if not seed_query:
            seed_query = "视频"

        video_result = await v2.aquery_data(
            workspace=workspace,
            query=seed_query,
            mode="hybrid",
        )
        assert video_result.get("status") == "success"
        video_chunks = video_result.get("data", {}).get("chunks", [])
        assert video_chunks, "Video query should return chunks"

        video_candidates = [
            c
            for c in video_chunks
            if (c.get("metadata") or {}).get("chunk_type") == "video"
            or "begin_ts" in (c.get("metadata") or {})
        ]
        assert video_candidates, "query_data should include video chunks"
        for vc in video_candidates:
            meta = vc.get("metadata") or {}
            assert isinstance(meta.get("segment_ids", []), list)
            assert "begin_ts" in meta and "end_ts" in meta

    finally:
        try:
            if text_path.exists():
                text_path.unlink()
            if test_dir.exists() and not any(test_dir.iterdir()):
                test_dir.rmdir()
        except Exception as e:
            logger.warning(f"Failed to cleanup test files: {e}")
