import json

import pytest

from hyperrag.contextual_embedder import ContextualEmbedder


@pytest.mark.asyncio
async def test_add_context_to_chunks_stores_llm_generated_chunk_title():
    async def llm_func(prompt: str) -> str:
        if "生成这个 chunk 的标题" in prompt:
            return "设备安装与操作"
        return "这是该片段的局部上下文说明。"

    embedder = ContextualEmbedder(llm_func=llm_func)
    chunks = [
        {
            "content": "请先连接电源，再按下启动按钮。",
            "doc_id": "doc-1",
            "order": 0,
            "metadata": {"chunk_type": "document"},
        }
    ]

    result = await embedder.add_context_to_chunks(chunks)

    content = json.loads(result[0]["content"])
    assert content["context"] == "这是该片段的局部上下文说明。"
    assert result[0]["metadata"]["chunk_type"] == "document"
    assert result[0]["metadata"]["context"] == "这是该片段的局部上下文说明。"
    assert result[0]["metadata"]["chunk_title"] == "设备安装与操作"
    assert isinstance(result[0]["metadata"]["chunk_title"], str)
    assert len(result[0]["metadata"]["chunk_title"]) <= 15


@pytest.mark.parametrize(
    ("raw_title", "expected"),
    [
        ("标题：这是一个超过十五个汉字的标题内容", "这是一个超过十五个汉字的标题内"),
        ('{"chunk_title": "视频操作说明"}', "视频操作说明"),
        ("not a Chinese title", None),
    ],
)
def test_normalize_chunk_title(raw_title: str, expected: str | None):
    assert ContextualEmbedder._normalize_chunk_title(raw_title) == expected
