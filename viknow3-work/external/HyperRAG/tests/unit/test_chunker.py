import asyncio
import json

import pytest

from hyperrag.chunker import HyperChunker
from hyperrag.parser import MinerUParser

# from hyperrag.schema import Asset, Segment


class _DummyTokenizer:
    def encode(self, text: str):
        # 简单的空白分词近似
        return (text or "").split()


def test_chunker_reads_sop_video_top_level_segments() -> None:
    content = json.dumps(
        {
            "sop_full": {
                "metadata": {
                    "semantic_type": "sop_video",
                    "title": "设备检查",
                    "sources": [
                        {
                            "source_id": "video-1",
                            "source_type": "video",
                            "source_url": "/video-1.mp4",
                        }
                    ],
                },
                "segments": [
                    {
                        "segment_id": "video-1_segment_0001",
                        "video_id": "video-1",
                        "time_range": [1.0, 4.0],
                        "data": {"caption": "操作员打开设备面板。"},
                    }
                ],
                "instructions": [
                    {
                        "step_id": "step-1",
                        "action_flow": [
                            {
                                "action_id": "action-1",
                                "segment_ids": ["video-1_segment_0001"],
                                "references": [],
                                "assertions": [],
                            }
                        ],
                    }
                ],
            }
        },
        ensure_ascii=False,
    )

    chunks = asyncio.run(
        HyperChunker()(
            tokenizer=_DummyTokenizer(),
            content=content,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    assert chunks
    assert any("video-1_segment_0001" in chunk["segment_ids"] for chunk in chunks)
    assert any("操作员打开设备面板" in chunk["content"] for chunk in chunks)


@pytest.mark.parametrize("token_size", [128, 256])
def test_chunker_on_mineru_output(token_size: int, tmp_path, mineru_output_factory):
    doc_id = "fixture-document"
    mineru_cache_dir = tmp_path / "mineru_cache"
    fake_file = tmp_path / f"{doc_id}.pdf"
    fake_file.write_bytes(b"PDF fixture")
    mineru_output_factory(mineru_cache_dir, fake_file)

    parser = MinerUParser()
    asset, segments = parser._load_existing_results(mineru_cache_dir, fake_file, doc_id)
    # 组装 KG 分块器需要的 JSON
    data = {
        "asset": {
            "doc_id": asset.doc_id,
            "source_uri": asset.source_uri,
            "modality": asset.modality,
            "title": asset.title,
            "language": asset.language,
            "segments_count": asset.segments_count,
        },
        "segments": [
            {
                "segment_id": s.segment_id,
                "doc_id": s.doc_id,
                "order": s.order,
                "type": s.type,
                "text": s.text,
                "location": s.location,
                "extra": s.extra,
            }
            for s in segments
        ],
    }
    content = json.dumps(data, ensure_ascii=False)

    chunker = HyperChunker()
    tokenizer = _DummyTokenizer()

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=content,
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=20,
            max_token_size=token_size,
        )
    )

    # 1) 所有 segments 都应在某个 chunk 的 segment_ids 中出现（覆盖）
    all_seg_ids = {s["segment_id"] for s in data["segments"]}
    covered = set()
    for ch in chunks:
        covered.update(ch.get("segment_ids", []))
        # 基本字段
        assert isinstance(ch.get("content"), str)
        assert isinstance(ch.get("tokens"), int)
        assert isinstance(ch.get("chunk_order_index"), int)
        # 2) 验证 content 的 token 不超过 token_size
        assert ch.get("tokens") <= token_size
    assert covered == all_seg_ids

    # 不同 token size 下 chunk 数量应有差异或保持合理（至少一个 chunk）
    assert len(chunks) >= 1
    long_text = max((s for s in segments if s.type == "text"), key=lambda s: len(s.text))
    assert sum(long_text.segment_id in ch.get("segment_ids", []) for ch in chunks) >= 2

    # 3) 模态独立性：遇到图片/表格不应合并；这些段应各自形成独立 chunk（文本 chunk 不包含模态段）
    modal_seg_ids = [
        s["segment_id"] for s in data["segments"] if s.get("type") in {"image", "table"}
    ]
    assert {s.type for s in segments} >= {"text", "image", "table"}
    for msid in modal_seg_ids:
        owners = [ch for ch in chunks if msid in ch.get("segment_ids", [])]
        assert len(owners) == 1
        owner = owners[0]
        # 该 chunk 中只应包含这个模态段
        assert owner.get("segment_ids", []) == [msid]


def _build_content(doc_id: str, segments: list[dict]) -> str:
    data = {
        "asset": {
            "doc_id": doc_id,
            "source_uri": f"/{doc_id}.md",
            "modality": "document",
            "title": doc_id,
            "language": "zh",
            "segments_count": len(segments),
        },
        "segments": segments,
    }
    return json.dumps(data, ensure_ascii=False)


def _build_video_content(doc_id: str, segments: list[dict]) -> str:
    data = {
        "asset": {
            "doc_id": doc_id,
            "source_uri": f"/{doc_id}.mp4",
            "modality": "video",
            "doc_type": "video",
            "title": doc_id,
            "language": "zh",
            "segments_count": len(segments),
            "extra": {
                "asset_id": doc_id,
                "video_id": doc_id,
                "source_id": doc_id,
                "source_parser": "video",
            },
        },
        "segments": segments,
    }
    return json.dumps(data, ensure_ascii=False)


def _find_chunk_by_segment_id(chunks: list[dict], sid: str) -> dict:
    owners = [ch for ch in chunks if sid in ch.get("segment_ids", [])]
    assert len(owners) == 1
    return owners[0]


def _chunk_raw_text(ch: dict) -> str:
    payload = json.loads(ch.get("content", "{}"))
    return str(payload.get("content", ""))


def test_runtime_token_count_does_not_use_character_or_experimental_unicode_size():
    class _WideTokenTokenizer:
        def encode(self, text: str):
            return [text] if text else []

    text = "中文和 ASCII 字符共同构成一个运行时 token"
    assert HyperChunker._runtime_token_count(_WideTokenTokenizer(), text) == 1


def test_文本在模态边界先flush_保持阅读顺序():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker()
    doc_id = "mix"
    segs = [
        {
            "segment_id": "t1",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "alpha beta gamma",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "img1",
            "doc_id": doc_id,
            "order": 1,
            "type": "image",
            "text": "image caption",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "t2",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "delta epsilon zeta",
            "location": {},
            "extra": {},
        },
    ]
    content = _build_content(doc_id, segs)
    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=content,
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    # 预期顺序：t1 文本先 flush，再 img1，最后 t2 文本
    ch_t1 = _find_chunk_by_segment_id(chunks, "t1")
    ch_img = _find_chunk_by_segment_id(chunks, "img1")
    ch_t2 = _find_chunk_by_segment_id(chunks, "t2")

    assert ch_t1.get("segment_ids", []) == ["t1"]
    assert ch_img.get("segment_ids", []) == ["img1"]
    assert ch_t2.get("segment_ids", []) == ["t2"]

    assert ch_t1["chunk_order_index"] < ch_img["chunk_order_index"] < ch_t2["chunk_order_index"]


@pytest.mark.parametrize("caption", ["片段一最终caption。", "片段一最终caption。" * 200, ""])
def test_video_clip_segments_are_atomic_text_chunks(caption):
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker()
    doc_id = "video-asset"
    segs = [
        {
            "segment_id": "clip-1",
            "doc_id": doc_id,
            "order": 0,
            "type": "video_clip",
            "text": caption,
            "location": {"begin_ts": 0, "end_ts": 30},
            "extra": {},
        },
        {
            "segment_id": "clip-2",
            "doc_id": doc_id,
            "order": 1,
            "type": "video_clip",
            "text": "片段二最终caption。动作线索: 展示特殊光路",
            "location": {"begin_ts": 30, "end_ts": 60},
            "extra": {},
        },
    ]

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_video_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    assert [chunk["segment_ids"] for chunk in chunks] == [["clip-1"], ["clip-2"]]
    first = _find_chunk_by_segment_id(chunks, "clip-1")
    first_text = _chunk_raw_text(first)
    assert first_text == caption
    assert first["metadata"]["chunk_type"] == "video"
    assert first["metadata"]["begin_ts"] == 0
    assert first["metadata"]["end_ts"] == 30


def test_video_chunk_metadata_flattens_main_struct():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker()
    doc_id = "video-asset"
    segments = [
        {
            "segment_id": "clip-1",
            "doc_id": doc_id,
            "order": 0,
            "type": "video_clip",
            "text": "视频描述",
            "main_struct": {
                "caption": "男子在室内解释。",
                "objects": [{"category": "男子", "state": "身穿绿色短袖"}],
                "ocr": {"ocr_text": "LES MYSTERES DE NEW-YORK", "ocr_keywords": ["NEW-YORK"]},
                "asr": {"text": "我已经邀请钱德勒了。"},
            },
            "location": {"begin_ts": 25.44, "end_ts": 29.04},
            "extra": {},
        }
    ]

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_video_content(doc_id, segments),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    metadata = chunks[0]["metadata"]
    assert _chunk_raw_text(chunks[0]) == "视频描述"
    assert metadata["caption"] == "男子在室内解释。"
    assert metadata["object"] == "男子，身穿绿色短袖"
    assert metadata["ocr"] == "LES MYSTERES DE NEW-YORK"
    assert metadata["asr"] == "我已经邀请钱德勒了。"


def test_模态边界短文本仅用_stage_a_补齐_不足直接输出():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker(
        enable_modal_replay=True,
        modal_flush_min_fill_ratio=0.8,
        modal_flush_min_fill_floor_tokens=8,
        modal_flush_replay_max_tokens=6,
    )
    doc_id = "stage-a-only"
    segs = [
        {
            "segment_id": "t0",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "a1 a2 a3 a4 a5 a6 a7 a8 a9 a10",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "img1",
            "doc_id": doc_id,
            "order": 1,
            "type": "image",
            "text": "image caption one",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "t1",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "b1 b2",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "img2",
            "doc_id": doc_id,
            "order": 3,
            "type": "image",
            "text": "image caption two",
            "location": {},
            "extra": {},
        },
    ]

    max_token_size = 20
    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=max_token_size,
        )
    )

    ch_t1 = _find_chunk_by_segment_id(chunks, "t1")
    ch_img2 = _find_chunk_by_segment_id(chunks, "img2")

    # 模态边界顺序：t1 先 flush，再输出 img2
    assert ch_t1["chunk_order_index"] < ch_img2["chunk_order_index"]

    # 原始 t1 只有 2 token，Stage A 回放后应变长
    assert ch_t1["tokens"] > 2

    # 仅使用 Stage A，不允许 Stage B 自扩展，因此 token 增长上限受 replay_max_tokens 限制
    assert ch_t1["tokens"] <= 2 + chunker.modal_flush_replay_max_tokens

    target_tokens = max(
        chunker.modal_flush_min_fill_floor_tokens,
        int(max_token_size * chunker.modal_flush_min_fill_ratio),
    )
    # Stage A 达到预算后仍不足目标，应直接输出（不做 Stage B）
    assert ch_t1["tokens"] < target_tokens

    # 回放内容来自上一个文本 chunk 的尾部
    t1_text = _chunk_raw_text(ch_t1)
    assert "b1 b2" in t1_text
    assert "a9" in t1_text or "a10" in t1_text


def test_modal_boundary_does_not_replay_previous_text_by_default():
    tokenizer = _DummyTokenizer()
    doc_id = "no-modal-replay"
    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "text",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "text",
                        "text": "上一段唯一标记。",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "image",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "image",
                        "text": "图片说明。",
                        "location": {},
                        "extra": {},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    text_chunk = _find_chunk_by_segment_id(chunks, "text")
    assert text_chunk["metadata"]["edited"] is False
    assert "上一段唯一标记" in _chunk_raw_text(text_chunk)


def test_语义边界不将新标题与上一节正文合并():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker(respect_semantic_boundaries=True)
    doc_id = "md"
    segs = [
        {
            "segment_id": "a",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "intro paragraph one",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "h1",
            "doc_id": doc_id,
            "order": 1,
            "type": "text",
            "text": "# Section Title\nThis is header paragraph",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "b",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "follow up paragraph",
            "location": {},
            "extra": {},
        },
    ]

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=256,
        )
    )

    assert _find_chunk_by_segment_id(chunks, "a")["segment_ids"] == ["a"]
    assert _find_chunk_by_segment_id(chunks, "h1")["segment_ids"] == ["h1", "b"]


def test_语义边界识别_mineru_转义_markdown_标题():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker(respect_semantic_boundaries=True)
    doc_id = "escaped-md"
    segs = [
        {
            "segment_id": "previous",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "previous section body",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "heading",
            "doc_id": doc_id,
            "order": 1,
            "type": "text",
            "text": "\\## New Section",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "current",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "current section body",
            "location": {},
            "extra": {},
        },
    ]

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=256,
        )
    )

    assert _find_chunk_by_segment_id(chunks, "previous")["segment_ids"] == ["previous"]
    assert _find_chunk_by_segment_id(chunks, "heading")["segment_ids"] == ["heading", "current"]


def test_parser_heading_segment_respects_section_boundary():
    tokenizer = _DummyTokenizer()
    doc_id = "parser-heading"
    segs = [
        {
            "segment_id": "previous",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "previous section body",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "heading",
            "doc_id": doc_id,
            "order": 1,
            "type": "heading",
            "text": "New Section",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "current",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "current section body",
            "location": {},
            "extra": {},
        },
    ]

    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=256,
        )
    )

    assert _find_chunk_by_segment_id(chunks, "previous")["segment_ids"] == ["previous"]
    assert _find_chunk_by_segment_id(chunks, "heading")["segment_ids"] == ["heading", "current"]


def test_模态边界不回放上一章节尾部():
    tokenizer = _DummyTokenizer()
    chunker = HyperChunker(
        modal_flush_min_fill_ratio=0.8,
        modal_flush_min_fill_floor_tokens=8,
        modal_flush_replay_max_tokens=6,
    )
    doc_id = "section-replay"
    segs = [
        {
            "segment_id": "previous",
            "doc_id": doc_id,
            "order": 0,
            "type": "text",
            "text": "previous section tail unique-marker",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "heading",
            "doc_id": doc_id,
            "order": 1,
            "type": "text",
            "text": "# New Section",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "current",
            "doc_id": doc_id,
            "order": 2,
            "type": "text",
            "text": "current section body",
            "location": {},
            "extra": {},
        },
        {
            "segment_id": "image",
            "doc_id": doc_id,
            "order": 3,
            "type": "image",
            "text": "image caption",
            "location": {},
            "extra": {},
        },
    ]

    chunks = asyncio.run(
        chunker(
            tokenizer=tokenizer,
            content=_build_content(doc_id, segs),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=20,
        )
    )

    current_chunk = _find_chunk_by_segment_id(chunks, "current")
    current_text = _chunk_raw_text(current_chunk)
    assert "current section body" in current_text
    assert "unique-marker" not in current_text
    assert current_chunk["metadata"]["edited"] is False


def test_short_heading_stays_with_first_body_chunk_when_body_is_split():
    tokenizer = _DummyTokenizer()
    doc_id = "split-section"
    body = " ".join(f"body-{index}" for index in range(20))
    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "previous",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "text",
                        "text": "previous section",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "heading",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "heading",
                        "text": "Current section",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "body",
                        "doc_id": doc_id,
                        "order": 2,
                        "type": "text",
                        "text": body,
                        "location": {},
                        "extra": {},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=8,
        )
    )

    heading_chunk = _find_chunk_by_segment_id(chunks, "heading")
    assert heading_chunk["segment_ids"] == ["heading", "body"]
    assert "Current section" in _chunk_raw_text(heading_chunk)
    assert "body-0" in _chunk_raw_text(heading_chunk)
    assert _find_chunk_by_segment_id(chunks, "previous")["segment_ids"] == ["previous"]
    assert all(chunk["tokens"] <= 8 for chunk in chunks)


def test_continuous_text_uses_o0_target_under_the_runtime_token_limit():
    tokenizer = _DummyTokenizer()
    text = " ".join(f"token-{index}" for index in range(20))
    chunks = asyncio.run(
        HyperChunker(
            enable_contextual_embeddings=False,
            continuous_text_target_tokens=8,
        )(
            tokenizer=tokenizer,
            content=_build_content(
                "o0-text-target",
                [
                    {
                        "segment_id": "text",
                        "doc_id": "o0-text-target",
                        "order": 0,
                        "type": "text",
                        "text": text,
                        "location": {},
                        "extra": {},
                    }
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=32,
        )
    )

    assert len(chunks) > 1
    assert all(chunk["tokens"] <= 8 for chunk in chunks)
    assert "".join(_chunk_raw_text(chunk) for chunk in chunks).replace(" ", "") == text.replace(
        " ", ""
    )


def test_empty_segment_is_provenance_only_not_synthetic_text():
    tokenizer = _DummyTokenizer()
    doc_id = "empty-segment"
    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "empty",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "text",
                        "text": "",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "body",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "text",
                        "text": "real PDF content",
                        "location": {},
                        "extra": {},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    assert len(chunks) == 1
    assert chunks[0]["segment_ids"] == ["empty", "body"]
    assert _chunk_raw_text(chunks[0]) == "real PDF content"
    assert "[空白文本]" not in _chunk_raw_text(chunks[0])


def test_long_markdown_table_repeats_its_header_and_stays_within_budget():
    tokenizer = _DummyTokenizer()
    doc_id = "long-table"
    table = "\n".join(
        [
            "| Key | Value |",
            "| --- | --- |",
            "| alpha | one |",
            "| bravo | two |",
            "| charlie | three |",
        ]
    )
    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "table",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "table",
                        "text": table,
                        "location": {},
                        "extra": {},
                    }
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=15,
        )
    )

    assert len(chunks) == 3
    assert all(chunk["segment_ids"] == ["table"] for chunk in chunks)
    assert all(chunk["tokens"] <= 15 for chunk in chunks)
    assert all(
        _chunk_raw_text(chunk).startswith("| Key | Value |\n| --- | --- |") for chunk in chunks
    )


def test_long_image_is_atomic_but_headerless_table_is_split_within_budget():
    tokenizer = _DummyTokenizer()
    doc_id = "image-table-budget"
    image_text = " ".join(f"image-{index}" for index in range(30))
    table_body = "".join(
        f"<tr><td>row-{index}</td><td>value-{index}</td></tr>" for index in range(12)
    )
    chunks = asyncio.run(
        HyperChunker(enable_contextual_embeddings=False)(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "image",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "image",
                        "text": image_text,
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "table",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "table",
                        "text": "large extracted table",
                        "location": {},
                        "extra": {"table_body": f"<table>{table_body}</table>"},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=12,
        )
    )

    image_chunks = [chunk for chunk in chunks if "image" in chunk["segment_ids"]]
    table_chunks = [chunk for chunk in chunks if "table" in chunk["segment_ids"]]
    assert len(image_chunks) == 1
    assert image_chunks[0]["tokens"] > 12
    assert _chunk_raw_text(image_chunks[0]) == image_text
    assert len(table_chunks) > 1
    assert all(chunk["tokens"] <= 12 for chunk in table_chunks)


def test_small_table_folded_into_text_preserves_its_caption():
    tokenizer = _DummyTokenizer()
    doc_id = "small-captioned-table"
    caption = "Quarterly sales summary"
    table = "| Region | Sales |\n| --- | --- |\n| East | 42 |"
    chunks = asyncio.run(
        HyperChunker(
            enable_contextual_embeddings=False,
            merge_small_tables_threshold_tokens=128,
        )(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "table",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "table",
                        "text": table,
                        "location": {},
                        "extra": {"caption": caption},
                    }
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    table_chunk = _find_chunk_by_segment_id(chunks, "table")
    assert table_chunk["tokens"] <= 128
    assert caption in _chunk_raw_text(table_chunk)
    assert table in _chunk_raw_text(table_chunk)


def test_headerless_source_table_remains_atomic_and_cannot_splice_topics():
    tokenizer = _DummyTokenizer()
    doc_id = "fused-boundary"
    chunks = asyncio.run(
        HyperChunker(
            enable_contextual_embeddings=False,
            merge_small_tables_threshold_tokens=128,
        )(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "lpddr-prose",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "text",
                        "text": "LPDDR and DDR5 have different system goals.",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "headerless-table",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "table",
                        "text": "[doc_title]DRAM[caption]LPDDR generations",
                        "location": {},
                        "extra": {
                            "caption": "[doc_title]DRAM[caption]LPDDR generations",
                            "table_body": (
                                "<table><tr><td>LPDDR4X</td><td>low power</td></tr>"
                                "<tr><td>LPDDR5</td><td>higher bandwidth</td></tr></table>"
                            ),
                        },
                    },
                    {
                        "segment_id": "next-topic",
                        "doc_id": doc_id,
                        "order": 2,
                        "type": "text",
                        "text": "Packaging and DIMM modules are a separate topic.",
                        "location": {},
                        "extra": {},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=128,
        )
    )

    table_chunk = _find_chunk_by_segment_id(chunks, "headerless-table")
    table_text = _chunk_raw_text(table_chunk)
    assert table_chunk["segment_ids"] == ["headerless-table"]
    assert table_chunk["metadata"]["chunk_type"] == "image"
    assert "Table rows:" in table_text
    assert "LPDDR4X" in table_text and "LPDDR5" in table_text
    assert "Packaging and DIMM" not in table_text
    assert _find_chunk_by_segment_id(chunks, "next-topic")["segment_ids"] == ["next-topic"]


def test_modal_flush_replays_prior_text_before_a_table_when_enabled():
    tokenizer = _DummyTokenizer()
    doc_id = "modal-table-replay"
    chunks = asyncio.run(
        HyperChunker(
            enable_contextual_embeddings=False,
            enable_modal_replay=True,
            modal_flush_min_fill_ratio=0.8,
            modal_flush_min_fill_floor_tokens=8,
            modal_flush_replay_max_tokens=4,
        )(
            tokenizer=tokenizer,
            content=_build_content(
                doc_id,
                [
                    {
                        "segment_id": "previous",
                        "doc_id": doc_id,
                        "order": 0,
                        "type": "text",
                        "text": "a1 a2 a3 a4 a5 a6 a7 a8",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "image",
                        "doc_id": doc_id,
                        "order": 1,
                        "type": "image",
                        "text": "image caption",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "current",
                        "doc_id": doc_id,
                        "order": 2,
                        "type": "text",
                        "text": "b1 b2",
                        "location": {},
                        "extra": {},
                    },
                    {
                        "segment_id": "table",
                        "doc_id": doc_id,
                        "order": 3,
                        "type": "table",
                        "text": "| Key | Value |\n| --- | --- |\n| item | value |",
                        "location": {},
                        "extra": {},
                    },
                ],
            ),
            split_by_character=None,
            split_by_character_only=False,
            overlap_token_size=0,
            max_token_size=15,
        )
    )

    current = _find_chunk_by_segment_id(chunks, "current")
    table = _find_chunk_by_segment_id(chunks, "table")
    assert current["metadata"]["edited"] is True
    assert "a7" in _chunk_raw_text(current) or "a8" in _chunk_raw_text(current)
    assert current["chunk_order_index"] < table["chunk_order_index"]
    assert table["segment_ids"] == ["table"]
    assert all(
        chunk["tokens"] <= 15 for chunk in chunks if chunk["metadata"]["chunk_type"] != "image"
    )
