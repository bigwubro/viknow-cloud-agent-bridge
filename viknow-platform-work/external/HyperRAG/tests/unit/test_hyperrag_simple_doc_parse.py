"""
HyperRAG 组件测试：文档解析流程（mineru + chunker）

直接测试文档解析流程（mineru + image_enhancer + chunker），不依赖完整的任务管理系统
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from loguru import logger

from hyperrag.chunker import HyperChunker

# 导入HyperRAG核心组件
from hyperrag.parser import MinerUParser


@pytest.mark.asyncio
async def test_doc_parse_mineru_chunker_flow(tmp_path, stub_mineru_command):
    """
    测试完整文档解析流程：
    1. MinerU 解析 PDF
    2. ImageEnhancer 增强（跳过，需要VLM）
    3. HyperChunker 分块
    """
    logger.info("=" * 60)
    logger.info("开始测试：文档解析流程（MinerU + HyperChunker）")
    logger.info("=" * 60)

    # 准备测试文档
    test_pdf = tmp_path / "document.pdf"
    test_pdf.write_bytes(b"PDF fixture")
    assert test_pdf.exists(), f"测试文档不存在: {test_pdf}"

    asset_id = "test-doc-001"
    logger.info(f"测试文档: {test_pdf}")
    logger.info(f"asset_id: {asset_id}")

    # 1. MinerU解析
    logger.info("\n步骤1: MinerU解析文档...")
    mineru_parser = MinerUParser()

    cache_dir = tmp_path / "mineru_cache" / asset_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    asset, segments = await asyncio.to_thread(
        mineru_parser.parse_document,
        asset_id,
        test_pdf,
        cache_dir,
        parser_params=dict(
            method="auto",
            lang="ch",
            # device="cpu",  # 强制使用CPU避免CUDA问题
            backend=None,
            formula=True,
            table=True,
        ),
    )

    logger.info(f"✓ 解析完成，获得 {len(segments)} 个segments")
    logger.info(f"  Asset: doc_id={asset.doc_id}, title={asset.title}")
    logger.info(f"  Segments样例: {segments[0] if segments else 'None'}")

    # 验证解析结果
    assert asset is not None
    assert asset.doc_id == asset_id
    assert len(segments) > 0
    assert len(stub_mineru_command) == 1

    # 2. ImageEnhancer增强（可选，需要VLM）
    logger.info("\n步骤2: ImageEnhancer增强（跳过VLM调用）...")
    # image_enhancer = ImageEnhancer(vision_model_func=None, max_concurrent_requests=1)
    # segments = await image_enhancer.enhance(asset, segments)
    logger.info("✓ 跳过VLM增强")

    # 3. HyperChunker分块
    logger.info("\n步骤3: HyperChunker分块...")

    # 准备 KG 输入格式
    import html

    index_input = {
        "asset": {
            "doc_id": asset.doc_id,
            "source_uri": asset.source_uri,
            "modality": asset.modality,
            "title": html.unescape(asset.title) if asset.title else None,
            "language": asset.language,
            "segments_count": asset.segments_count,
        },
        "segments": [
            {
                "segment_id": seg.segment_id,
                "doc_id": seg.doc_id,
                "order": seg.order,
                "type": seg.type,
                "text": html.unescape(seg.text),
                "location": seg.location,
                "extra": {
                    k: html.unescape(v) if isinstance(v, str) else v for k, v in seg.extra.items()
                },
            }
            for seg in segments
        ],
    }
    content = json.dumps(index_input, ensure_ascii=False)

    logger.info(f"  准备好的 KG 输入长度: {len(content)} 字符")

    # 创建HyperChunker（不启用contextual embeddings）
    chunker = HyperChunker(
        enable_contextual_embeddings=False,
        contextual_llm_func=None,
        contextual_max_tokens=100,
        contextual_window_chunks=2,
    )

    # 执行分块
    # HyperChunker的签名是: def __call__(self, text: str, chunk_size: int) -> List[str]
    chunk_size = 512  # tokens
    import tiktoken

    tokenizer = tiktoken.encoding_for_model("gpt-4o")
    chunks = await chunker(tokenizer, content, max_token_size=chunk_size)

    logger.info(f"✓ 分块完成，获得 {len(chunks)} 个chunks")
    if chunks:
        logger.info(f"  第一个chunk长度: {len(chunks[0])} 字符")
        logger.info(f"  第一个chunk预览: {chunks[0]['content'][:200]}...")

    # 验证分块结果
    assert len(chunks) > 0
    assert all(isinstance(chunk["content"], str) for chunk in chunks)

    # 4. 解析chunks以验证结构
    logger.info("\n步骤4: 验证chunk结构...")
    parsed_chunks = []
    for i, chunk in enumerate(chunks):
        try:
            parsed = json.loads(chunk["content"])
            parsed_chunks.append(parsed)
            if i == 0:
                logger.info(f"  第一个chunk解析结果: {list(parsed.keys())}")
        except json.JSONDecodeError as e:
            logger.warning(f"  Chunk {i} JSON解析失败: {e}")

    logger.info(f"✓ 成功解析 {len(parsed_chunks)}/{len(chunks)} 个chunks")

    # 5. 统计信息
    logger.info("\n=" * 60)
    logger.info("测试完成 - 统计信息:")
    logger.info(f"  - 原始segments: {len(segments)}")
    logger.info(f"  - 分块后chunks: {len(chunks)}")
    logger.info(f"  - 平均每个chunk长度: {sum(len(c) for c in chunks) / len(chunks):.0f} 字符")
    logger.info("=" * 60)

    # 验证完整流程
    assert len(chunks) > 0
    assert len(parsed_chunks) > 0

    logger.info("✅ 所有测试通过！")


@pytest.mark.asyncio
async def test_mineru_parser_basic(tmp_path, stub_mineru_command):
    """测试MinerU Parser的基本功能"""
    logger.info("测试MinerU Parser基本功能...")

    test_pdf = tmp_path / "document.pdf"
    test_pdf.write_bytes(b"PDF fixture")
    assert test_pdf.exists()

    parser = MinerUParser()

    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_dir = Path(tmp_dir) / "test_cache"

        asset, segments = await asyncio.to_thread(
            parser.parse_document,
            "test-001",
            test_pdf,
            cache_dir,
            parser_params={"method": "auto", "lang": "ch"},
        )

        assert asset is not None
        assert len(segments) > 0
        assert len(stub_mineru_command) == 1
        logger.info(f"✓ MinerU解析成功: {len(segments)} segments")


@pytest.mark.asyncio
async def test_hyper_chunker_basic():
    """测试HyperChunker的基本功能"""
    logger.info("测试HyperChunker基本功能...")

    # 准备测试文本
    test_text = json.dumps(
        {
            "asset": {"doc_id": "test-001", "title": "Test Document"},
            "segments": [
                {
                    "segment_id": "s1",
                    "doc_id": "test-001",
                    "order": 0,
                    "type": "text",
                    "text": "这是第一段" * 100,
                },
                {
                    "segment_id": "s2",
                    "doc_id": "test-001",
                    "order": 1,
                    "type": "text",
                    "text": "这是第二段" * 100,
                },
            ],
        },
        ensure_ascii=False,
    )

    chunker = HyperChunker(enable_contextual_embeddings=False)
    import tiktoken

    tokenizer = tiktoken.encoding_for_model("gpt-4o")
    chunks = await chunker(tokenizer, test_text, max_token_size=200)

    assert len(chunks) > 0
    logger.info(f"✓ HyperChunker分块成功: {len(chunks)} chunks")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v", "--log-cli-level=INFO", "--run-integration"]))
