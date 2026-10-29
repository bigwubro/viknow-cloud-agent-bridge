import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hyperrag import HyperRAGConfig, HyperRAGV2
from hyperrag.rerank import RerankResult


class _DummyObservation:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def update(self, **kwargs):
        self.payload = kwargs


class _DummyLangfuseClient:
    def start_as_current_observation(self, **kwargs):
        return _DummyObservation()


@pytest.mark.asyncio
async def test_aquery_data_request_rerank_overrides_config_and_reranks_merged_chunks(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=False,
    )
    hyperrag = HyperRAGV2(config)

    text_chunk = {"chunk_id": "text-1", "content": "text result"}
    video_chunk = {
        "chunk_id": "video-1",
        "content": "video result",
        "metadata": {"retrieved_by": "video_embedding"},
    }

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: _DummyLangfuseClient())
    hyperrag._query_hybrid = AsyncMock(
        return_value={
            "status": "success",
            "data": {"chunks": [text_chunk, video_chunk], "entities": [], "relationships": []},
            "langfuse_trace": {},
        }
    )
    hyperrag._rerank_query_chunks = AsyncMock(return_value=[video_chunk, text_chunk])

    result = await hyperrag.aquery_data(
        workspace="test-workspace",
        query="怎么进入轿顶",
        retrieval_mode="hybrid",
        rerank_enabled=True,
    )

    assert result["data"]["chunks"] == [video_chunk, text_chunk]
    hyperrag._rerank_query_chunks.assert_awaited_once_with(
        workspace="test-workspace",
        query="怎么进入轿顶",
        chunks=[text_chunk, video_chunk],
        rerank_top_k=50,
    )


@pytest.mark.asyncio
async def test_aquery_data_deduplicates_video_candidates_before_rerank_top_k(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_top_k=2,
    )
    hyperrag = HyperRAGV2(config)
    base_chunk = {"chunk_id": "chunk-1", "content": "text result", "score": 0.2}
    duplicate_video_chunk = {
        "chunk_id": "video-1",
        "content": "duplicate video result",
        "score": 0.9,
        "metadata": {
            "chunk_id": "chunk-1",
            "retrieved_by": "video_embedding",
        },
    }
    unique_video_chunk = {
        "chunk_id": "video-2",
        "content": "unique video result",
        "metadata": {
            "chunk_id": "chunk-2",
            "retrieved_by": "video_embedding",
        },
    }

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: _DummyLangfuseClient())
    hyperrag._query_hybrid = AsyncMock(
        return_value={
            "status": "success",
            "data": {
                "chunks": [base_chunk, duplicate_video_chunk, unique_video_chunk],
                "entities": [],
                "relationships": [],
            },
            "langfuse_trace": {},
        }
    )
    hyperrag._query_vkr_delta_graph = AsyncMock(
        return_value={"chunks": [], "entities": [], "relationships": [], "stats": {}}
    )
    hyperrag._rerank_query_chunks = AsyncMock(side_effect=lambda **kwargs: kwargs["chunks"])

    result = await hyperrag.aquery_data(
        workspace="test-workspace",
        query="电动车",
        retrieval_mode="hybrid",
    )

    assert result["data"]["chunks"] == [base_chunk, unique_video_chunk]
    hyperrag._rerank_query_chunks.assert_awaited_once_with(
        workspace="test-workspace",
        query="电动车",
        chunks=[base_chunk, unique_video_chunk],
        rerank_top_k=2,
    )


@pytest.mark.asyncio
async def test_aquery_data_uses_config_rerank_default_when_request_is_none(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.0,
    )
    hyperrag = HyperRAGV2(config)

    text_chunk = {"chunk_id": "text-1", "content": "text result"}

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: _DummyLangfuseClient())
    hyperrag._query_hybrid = AsyncMock(
        return_value={
            "status": "success",
            "data": {"chunks": [text_chunk], "entities": [], "relationships": []},
            "langfuse_trace": {},
        }
    )
    hyperrag.aquery_video_chunks = AsyncMock(return_value=[])
    hyperrag._rerank_query_chunks = AsyncMock(return_value=[text_chunk])

    await hyperrag.aquery_data(
        workspace="test-workspace",
        query="怎么进入轿顶",
        retrieval_mode="hybrid",
    )

    assert hyperrag._rerank_query_chunks.await_count == 1


@pytest.mark.asyncio
async def test_aquery_data_request_can_disable_config_default_rerank(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.0,
    )
    hyperrag = HyperRAGV2(config)

    text_chunk = {"chunk_id": "text-1", "content": "text result"}

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: _DummyLangfuseClient())
    hyperrag._query_hybrid = AsyncMock(
        return_value={
            "status": "success",
            "data": {"chunks": [text_chunk], "entities": [], "relationships": []},
            "langfuse_trace": {},
        }
    )
    hyperrag.aquery_video_chunks = AsyncMock(return_value=[])
    hyperrag._rerank_query_chunks = AsyncMock(return_value=[text_chunk])

    result = await hyperrag.aquery_data(
        workspace="test-workspace",
        query="怎么进入轿顶",
        retrieval_mode="hybrid",
        rerank_enabled=False,
    )

    assert result["data"]["chunks"] == [text_chunk]
    hyperrag._rerank_query_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerank_query_chunks_times_out_and_keeps_original_order(monkeypatch) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_timeout_seconds=0.01,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {"chunk_id": "first", "content": "first result"},
        {"chunk_id": "second", "content": "second result"},
    ]

    class HangingReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            await asyncio.sleep(10)
            return []

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", HangingReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
    )

    assert result == chunks


@pytest.mark.asyncio
async def test_rerank_query_chunks_defaults_to_text_without_preparing_media(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.0,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {
            "chunk_id": "first",
            "content": "first result",
            "metadata": {"retrieved_by": "video_embedding"},
        },
        {"chunk_id": "second", "content": "second result"},
    ]
    called_modes = []

    def fail_media(*args, **kwargs):
        raise AssertionError("text rerank should not prepare multimodal media")

    class TextReranker:
        def __init__(self, **kwargs):
            self.mode = kwargs["mode"]
            called_modes.append(self.mode)

        async def rerank(self, query, candidates):
            assert self.mode == "text"
            assert all(not candidate.media for candidate in candidates)
            assert candidates[0].text == "<content>first result</content>"
            assert candidates[1].text == "<content>second result</content>"
            return [
                RerankResult(candidate_id="1", index=1, relevance_score=0.9),
                RerankResult(candidate_id="0", index=0, relevance_score=0.1),
            ]

    monkeypatch.setattr(
        HyperRAGV2,
        "_chunk_rerank_media",
        staticmethod(fail_media),
    )
    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", TextReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
    )

    assert called_modes == ["text"]
    assert [chunk["chunk_id"] for chunk in result] == ["second", "first"]


def test_chunk_rerank_text_uses_tagged_fields() -> None:
    text = HyperRAGV2._chunk_rerank_text(
        {
            "content": "动作: 打开设备面板",
            "context": "维修流程",
            "metadata": {
                "title": "SX-70 教程",
                "embedding_text": (
                    "<caption>画面显示操作员打开设备面板。</caption>\n"
                    "<ocr_keywords>SX-70；POWER</ocr_keywords>"
                ),
            },
        }
    )

    assert "<title>SX-70 教程</title>" in text
    assert "<caption>画面显示操作员打开设备面板。</caption>" in text
    assert "<ocr_keywords>SX-70；POWER</ocr_keywords>" in text
    assert "<content>动作: 打开设备面板</content>" in text
    assert "<context>维修流程</context>" in text


@pytest.mark.asyncio
async def test_rerank_query_chunks_falls_back_to_text_rerank_on_multimodal_timeout(
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_mode="multimodal",
        rerank_timeout_seconds=0.01,
        rerank_score_threshold=0.0,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {
            "chunk_id": "first",
            "content": "first result",
            "metadata": {"retrieved_by": "video_embedding"},
        },
        {
            "chunk_id": "second",
            "content": "second result",
            "metadata": {"retrieved_by": "video_embedding"},
        },
    ]
    called_modes = []

    class FallbackReranker:
        def __init__(self, **kwargs):
            self.mode = kwargs["mode"]
            called_modes.append(self.mode)

        async def rerank(self, query, candidates):
            if self.mode == "multimodal":
                await asyncio.sleep(10)
            assert all(not candidate.media for candidate in candidates)
            return [
                RerankResult(candidate_id="1", index=1, relevance_score=0.9),
                RerankResult(candidate_id="0", index=0, relevance_score=0.1),
            ]

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", FallbackReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
    )

    assert called_modes == ["multimodal", "text"]
    assert [chunk["chunk_id"] for chunk in result] == ["second", "first"]
    assert result[0]["metadata"]["rerank_score"] == 0.9


@pytest.mark.asyncio
async def test_rerank_query_chunks_uses_video_clip_for_timed_video_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_mode="multimodal",
    )
    hyperrag = HyperRAGV2(config)
    source_video = tmp_path / "SX-70.mp4"
    source_video.write_bytes(b"source video")
    captured_media_paths = []

    def fake_extract_clip(*, source_path, begin_ts, end_ts, output_dir):
        assert source_path == source_video
        assert begin_ts == 120.0
        assert end_ts == 150.0
        clip_path = Path(output_dir) / "SX-70_120.000_150.000.mp4"
        clip_path.write_bytes(b"clip video")
        return clip_path

    class CapturingReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            media = list(candidates[0].media)
            assert media
            assert media[0].path is not None
            captured_media_paths.append(Path(media[0].path))
            assert captured_media_paths[0].is_file()
            assert captured_media_paths[0] != source_video
            return []

    monkeypatch.setattr(
        HyperRAGV2,
        "_extract_rerank_video_clip",
        staticmethod(fake_extract_clip),
    )
    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", CapturingReranker)

    chunks = [
        {
            "chunk_id": "video-1",
            "content": "video result",
            "file_path": str(source_video),
            "metadata": {
                "chunk_type": "video",
                "retrieved_by": "video_embedding",
                "begin_ts": 120.0,
                "end_ts": 150.0,
            },
        }
    ]

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
    )

    assert result == chunks
    assert captured_media_paths
    assert not captured_media_paths[0].exists()


@pytest.mark.asyncio
async def test_rerank_query_chunks_discards_non_candidate_chunks_by_top_k(monkeypatch) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_top_k=1,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {"chunk_id": "first", "content": "first result"},
        {"chunk_id": "second", "content": "second result"},
    ]

    class TextReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            return [RerankResult(candidate_id="0", index=0, relevance_score=0.9)]

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", TextReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
        rerank_top_k=1,
    )

    assert [chunk["chunk_id"] for chunk in result] == ["first"]


@pytest.mark.asyncio
async def test_rerank_query_chunks_applies_score_threshold(monkeypatch) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.55,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {"chunk_id": "high", "content": "high relevance"},
        {"chunk_id": "low", "content": "low relevance"},
    ]

    class ThresholdReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            return [
                RerankResult(candidate_id="0", index=0, relevance_score=0.8),
                RerankResult(candidate_id="1", index=1, relevance_score=0.2),
            ]

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", ThresholdReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
        rerank_top_k=2,
    )

    assert [chunk["chunk_id"] for chunk in result] == ["high"]


@pytest.mark.asyncio
async def test_rerank_query_chunks_drops_all_when_scores_below_threshold(monkeypatch) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.55,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {"chunk_id": "low-1", "content": "low relevance 1"},
        {"chunk_id": "low-2", "content": "low relevance 2"},
    ]

    class ThresholdReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            return [
                RerankResult(candidate_id="0", index=0, relevance_score=0.3),
                RerankResult(candidate_id="1", index=1, relevance_score=0.2),
            ]

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", ThresholdReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
        rerank_top_k=2,
    )

    assert result == []


@pytest.mark.asyncio
async def test_rerank_query_chunks_uses_request_threshold_override(monkeypatch) -> None:
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        rerank_enabled=True,
        rerank_score_threshold=0.55,
    )
    hyperrag = HyperRAGV2(config)
    chunks = [
        {"chunk_id": "candidate-1", "content": "candidate 1"},
        {"chunk_id": "candidate-2", "content": "candidate 2"},
    ]

    class ThresholdReranker:
        def __init__(self, **kwargs):
            pass

        async def rerank(self, query, candidates):
            return [
                RerankResult(candidate_id="0", index=0, relevance_score=0.4),
                RerankResult(candidate_id="1", index=1, relevance_score=0.2),
            ]

    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", ThresholdReranker)

    result = await hyperrag._rerank_query_chunks(
        workspace="test-workspace",
        query="SX70 光路",
        chunks=chunks,
        rerank_top_k=2,
        score_threshold=0.3,
    )

    assert [chunk["chunk_id"] for chunk in result] == ["candidate-1"]
