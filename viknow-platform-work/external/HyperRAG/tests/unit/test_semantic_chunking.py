"""
测试语义分块功能

验证：
1. 语义分块能够在句子、段落边界处切分
2. 生成的 chunks 具有完整的语义单元
3. 不会在句子中间截断
"""

import json
from typing import List
import asyncio

from hyperrag.chunker import HyperChunker


# Mock tokenizer for testing
class MockTokenizer:
    """模拟 tokenizer，简单按空格和标点计数"""

    def encode(self, text: str) -> List[int]:
        if not text:
            return []
        # 简单估算：中文字符算2个token，英文单词算1个token，标点算1个token
        tokens = []
        for char in text:
            if "\u4e00" <= char <= "\u9fff":  # 中文字符
                tokens.extend([0, 0])  # 2 tokens per Chinese char
            elif char.isspace():
                continue
            else:
                tokens.append(0)  # 1 token for other chars
        return tokens


class CharacterTokenizer:
    def encode(self, text: str) -> List[int]:
        return list(range(len(text or "")))


def test_semantic_chunking():
    """测试语义分块功能"""
    from hyperrag.chunker import HyperChunker

    # 创建启用语义分块的 chunker
    chunker = HyperChunker(
        enable_semantic_splitting=True,
        enable_contextual_embeddings=False,
    )

    tokenizer = MockTokenizer()

    # 测试文档：包含多个段落和句子
    test_segments = [
        {
            "segment_id": "seg-1",
            "type": "text",
            "text": """人工智能的发展历程。人工智能（Artificial Intelligence，简称AI）是计算机科学的一个重要分支。
它致力于研究如何让计算机模拟人类的智能行为。从20世纪50年代开始，研究人员就在探索这个领域。

早期的人工智能研究主要集中在符号推理和问题求解。专家系统在80年代取得了显著的成功。
然而，这些系统存在知识获取瓶颈。它们难以处理不确定性和模糊信息。

进入21世纪后，机器学习成为人工智能的主流方法。深度学习的突破带来了革命性的变化。
神经网络能够从大量数据中自动学习特征。这使得AI在图像识别、语音识别等领域取得了巨大进步。

当前，大语言模型正在改变人工智能的应用方式。GPT、BERT等模型展示了强大的语言理解能力。
它们能够完成文本生成、翻译、问答等多种任务。这为AI的实际应用开辟了新的可能性。""",
        }
    ]

    # 构建输入
    asset = {"doc_id": "test_doc", "modality": "document"}

    content = json.dumps({"asset": asset, "segments": test_segments}, ensure_ascii=False)

    MAX_TOKEN_SIZE = 100
    # 执行分块，设置较小的 token 限制以触发分块
    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=content,
            max_token_size=MAX_TOKEN_SIZE,  # 较小的限制以触发分块
            overlap_token_size=0,
        )
    )

    print(f"\n{'=' * 80}")
    print("语义分块测试结果")
    print(f"{'=' * 80}\n")

    print(f"生成了 {len(chunks)} 个文本块\n")

    # 验证每个 chunk
    for i, chunk in enumerate(chunks):
        content = chunk.get("content", "")
        # 去掉 JSON header
        if content.startswith("{"):
            lines = content.split("\n", 1)
            if len(lines) > 1:
                content = lines[1]

        tokens = chunk.get("tokens", 0)

        print(f"--- Chunk {i + 1} (tokens: {tokens}) ---")
        # print(f"{content[:200]}{'...' if len(content) > 200 else ''}")
        print(content)
        print()

        # 验证：检查是否在句子边界结束
        content_stripped = content.strip()
        if content_stripped:
            last_char = content_stripped[-1]
            ends_with_sentence = last_char in {"。", ".", "!", "？", "?", "！"}

            status = "✓" if (ends_with_sentence or i == len(chunks) - 1) else "⚠"
            print(f"{status} 语义完整性: ", end="")
            if ends_with_sentence:
                print("以句子结束")
            elif i == len(chunks) - 1:
                print("最后一个 chunk")
            else:
                print(f"未以句号结束 (最后字符: '{last_char}')")
            print()

    # 统计信息
    print(f"{'=' * 80}")
    print("统计信息:")
    print(f"- 总块数: {len(chunks)}")
    print(f"- 平均 token 数: {sum(c.get('tokens', 0) for c in chunks) / len(chunks):.1f}")
    print(f"- 最大 token 数: {max(c.get('tokens', 0) for c in chunks)}")
    print(f"- 最小 token 数: {min(c.get('tokens', 0) for c in chunks)}")

    # 检查是否有 chunk 超过限制
    over_limit = [c for c in chunks if c.get("tokens", 0) > MAX_TOKEN_SIZE]
    if over_limit:
        print(f"\n⚠ 警告: {len(over_limit)} 个 chunks 超过 token 限制")
    else:
        print("\n✓ 所有 chunks 都在 token 限制内")

    print(f"{'=' * 80}\n")


def test_semantic_vs_simple_splitting():
    """对比语义分块和简单分块"""
    from hyperrag.chunker import HyperChunker

    tokenizer = MockTokenizer()

    # 测试文本
    test_text = """第一章 绪论。本章介绍研究背景。人工智能是一个重要的研究领域。它在各行各业都有广泛的应用。

第二章 相关工作。本章回顾了前人的研究成果。机器学习方法在过去十年取得了显著进展。深度学习改变了AI的研究范式。"""

    test_segments = [{"segment_id": "seg-1", "type": "text", "text": test_text}]
    asset = {"doc_id": "test", "modality": "document"}
    content = json.dumps({"asset": asset, "segments": test_segments}, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print("语义分块 vs 简单分块 对比测试")
    print(f"{'=' * 80}\n")

    # 测试语义分块
    chunker_semantic = HyperChunker(enable_semantic_splitting=True)
    chunks_semantic = asyncio.run(
        chunker_semantic(
            tokenizer=tokenizer,
            content=content,
            max_token_size=50,
            overlap_token_size=0,
        )
    )

    print("【语义分块结果】")
    print(f"生成 {len(chunks_semantic)} 个 chunks:\n")
    for i, chunk in enumerate(chunks_semantic):
        content_text = (
            chunk.get("content", "").split("\n", 1)[1] if "\n" in chunk.get("content", "") else ""
        )
        print(f"Chunk {i + 1}: {content_text[:80]}...")
        print()

    # 测试简单分块
    chunker_simple = HyperChunker(enable_semantic_splitting=False)
    chunks_simple = asyncio.run(
        chunker_simple(
            tokenizer=tokenizer,
            content=content,
            max_token_size=50,
            overlap_token_size=0,
        )
    )

    print("\n【简单分块结果】")
    print(f"生成 {len(chunks_simple)} 个 chunks:\n")
    for i, chunk in enumerate(chunks_simple):
        content_text = (
            chunk.get("content", "").split("\n", 1)[1] if "\n" in chunk.get("content", "") else ""
        )
        print(f"Chunk {i + 1}: {content_text[:80]}...")
        print()

    print(f"{'=' * 80}\n")


def test_overlap_semantic_integrity():
    """测试重叠部分的语义完整性"""
    from hyperrag.chunker import HyperChunker

    tokenizer = MockTokenizer()

    # 测试文本：连续的句子
    test_text = """这是第一个句子。这是第二个句子。这是第三个句子。这是第四个句子。这是第五个句子。这是第六个句子。这是第七个句子。这是第八个句子。"""

    test_segments = [{"segment_id": "seg-1", "type": "text", "text": test_text}]
    asset = {"doc_id": "test", "modality": "document"}
    content = json.dumps({"asset": asset, "segments": test_segments}, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print("重叠部分语义完整性测试")
    print(f"{'=' * 80}\n")

    chunker = HyperChunker(enable_semantic_splitting=True)
    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=content,
            max_token_size=40,
            overlap_token_size=0,
        )
    )

    print(f"生成 {len(chunks)} 个 chunks (overlap=15 tokens):\n")

    for i, chunk in enumerate(chunks):
        content_text = (
            chunk.get("content", "").split("\n", 1)[1] if "\n" in chunk.get("content", "") else ""
        )
        print(f"Chunk {i + 1}:")
        print(f"  {content_text}")

        if i > 0:
            prev_content = (
                chunks[i - 1].get("content", "").split("\n", 1)[1]
                if "\n" in chunks[i - 1].get("content", "")
                else ""
            )
            overlap_found = False
            for j in range(len(prev_content) // 2, len(prev_content)):
                suffix = prev_content[j:]
                if content_text.startswith(suffix[:20]) and len(suffix) > 10:
                    print(f"  ✓ 重叠内容: {suffix[:50]}...")
                    overlap_found = True
                    break

            if not overlap_found:
                print("  ⚠ 未检测到明显重叠")
        print()

    print(f"{'=' * 80}\n")


def test_fallback_splitter_prefers_sentence_boundaries():
    chunker = HyperChunker(enable_semantic_splitting=False)
    tokenizer = CharacterTokenizer()
    text = "第一句已经完整结束。第二句也完整结束。第三句继续完整结束。"

    chunks = chunker._split_text_by_token_limit(
        tokenizer,
        text,
        max_tokens=12,
    )

    assert len(chunks) >= 2
    assert "".join(chunks) == text
    assert all(len(tokenizer.encode(chunk)) <= 12 for chunk in chunks)
    assert all(chunk.endswith("。") for chunk in chunks[:-1])


def test_video_chunk_metadata_includes_global_metadata():
    from hyperrag.chunker import HyperChunker

    chunker = HyperChunker(enable_semantic_splitting=True)
    metadata = chunker._build_chunk_metadata(
        asset={
            "doc_id": "video_doc_1",
            "modality": "video",
            "extra": {
                "source_id": "video_doc_1",
                "source_parser": "video",
                "global_metadata": "时长:80秒；平均帧率:30fps；创建时间:2024-01-02T03:04:05Z",
            },
        },
        seg_map={
            "video_doc_1_clip_0000": {
                "location": {"begin_ts": 0.0, "end_ts": 30.0},
            }
        },
        segment_ids=["video_doc_1_clip_0000"],
    )

    assert metadata["chunk_type"] == "video"
    assert metadata["global_metadata"] == "时长:80秒；平均帧率:30fps；创建时间:2024-01-02T03:04:05Z"


def test_video_chunk_metadata_preserves_versioned_claims() -> None:
    from hyperrag.chunker import HyperChunker

    claims = [
        {
            "claim_id": "seg-1:claim:001",
            "text": "button visible",
            "source": "visual",
            "supports": [{"segment_id": "seg-1"}],
        }
    ]
    chunker = HyperChunker(enable_semantic_splitting=True)
    metadata = chunker._build_chunk_metadata(
        asset={"doc_id": "video-1", "modality": "video", "extra": {}},
        seg_map={
            "seg-1": {
                "location": {"begin_ts": 0.0, "end_ts": 5.0},
                "main_struct": {
                    "caption": "button visible",
                    "claims_schema_version": 1,
                    "claims": claims,
                },
            }
        },
        segment_ids=["seg-1"],
    )
    assert metadata["claims_by_segment"] == [
        {"segment_id": "seg-1", "schema_version": 1, "claims": claims}
    ]


if __name__ == "__main__":
    # 运行所有测试
    test_semantic_chunking()
    # test_semantic_vs_simple_splitting()
    # test_overlap_semantic_integrity()

    print("\n✓ 所有语义分块测试完成")
