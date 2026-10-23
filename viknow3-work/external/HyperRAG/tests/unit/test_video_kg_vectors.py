import asyncio
from unittest.mock import AsyncMock

import pytest
from hyperrag.config_loader import HyperRAGConfig
from hyperrag.single_layer_kg import ChunkRecord, DocumentGraph, SingleLayerKGService


def video_graph(tmp_path):
    segments = [
        {
            "segment_id": f"seg-{i}",
            "type": "video_clip",
            "main_struct": {"caption": f"action {i}", "ocr": {"ocr_keywords": ["panel"]}},
            "location": {"begin_ts": i, "end_ts": i + 1},
            "extra": {},
        }
        for i in range(2)
    ]
    chunks = [
        ChunkRecord(
            chunk_id=f"chunk-{i}",
            file_path=str(tmp_path / "source.mp4"),
            order=i,
            raw_content=f"context {i}",
            text=f"context {i}",
            tokens=2,
            segment_ids=[f"seg-{i}"],
            metadata={"chunk_type": "video", "claims_by_segment": [{"segment_id": f"seg-{i}"}]},
        )
        for i in range(2)
    ]
    return DocumentGraph(
        workspace="ws",
        file_path=str(tmp_path / "source.mp4"),
        source_uri="source.mp4",
        modality="video",
        title="video",
        asset={"doc_id": "asset"},
        segments=segments,
        chunks=chunks,
        extracted_entities=[],
        extracted_relations=[],
        entity_nodes=[],
        relation_edges=[],
    )


@pytest.mark.asyncio
async def test_video_chunks_have_one_mixed_vector_and_no_text_embedding(tmp_path, monkeypatch):
    graph = video_graph(tmp_path)
    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2))
    service.embedding_client = AsyncMock()
    service.multimodal_embedder = AsyncMock()
    service.multimodal_embedder.embed_video.side_effect = [[1.0, 0.0], [0.0, 1.0]]
    paths = []

    def resolve(**kwargs):
        paths.append(kwargs["segment_id"])
        return f"{kwargs['segment_id']}.mp4"

    monkeypatch.setattr(
        "hyperrag.single_layer_kg.resolve_segment_video_path", resolve, raising=False
    )
    rows = await service._build_chunk_vector_rows(graph)

    service.embedding_client.embed_texts.assert_not_awaited()
    assert paths == ["seg-0", "seg-1"]
    assert [row["item_id"] for row in rows] == ["chunk-0", "chunk-1"]
    assert [row["embedding"] for row in rows] == [[1.0, 0.0], [0.0, 1.0]]
    assert rows[0]["metadata"]["metadata"] == graph.chunks[0].metadata
    calls = service.multimodal_embedder.embed_video.await_args_list
    assert len(calls) == 2
    assert calls[0].args == ("seg-0.mp4",)
    assert calls[0].kwargs == {
        "task": "video_indexing",
        "text": "<caption>action 0 OCR: panel</caption>\n<ocr_keywords>panel</ocr_keywords>",
    }
    assert rows[0]["content"] == "context 0"


@pytest.mark.asyncio
async def test_non_video_chunks_still_use_batched_text_encoding(tmp_path):
    graph = video_graph(tmp_path)
    graph.modality = "document"
    graph.segments = []
    for chunk in graph.chunks:
        chunk.metadata = {}
    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2))
    service.embedding_client = AsyncMock()
    service.embedding_client.embed_texts.return_value = [[1.0, 0.0], [0.0, 1.0]]
    rows = await service._build_chunk_vector_rows(graph)
    service.embedding_client.embed_texts.assert_awaited_once_with(["context 0", "context 1"])
    assert len(rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_video_encoding_obeys_shared_kg_embedding_concurrency(tmp_path, monkeypatch, limit):
    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2, embedding_max_async=limit))
    active = 0
    peak = 0

    async def embed_video(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return [1.0, 0.0]
        finally:
            active -= 1

    service.multimodal_embedder = AsyncMock()
    service.multimodal_embedder.embed_video.side_effect = embed_video
    monkeypatch.setattr(
        "hyperrag.single_layer_kg.resolve_segment_video_path",
        lambda **kwargs: f"{kwargs['segment_id']}.mp4",
    )
    rows = await service._build_chunk_vector_rows(video_graph(tmp_path))
    assert peak == limit
    assert len(rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "duplicate", "dimension", "endpoint"])
async def test_invalid_video_encoding_fails_instead_of_text_fallback(
    tmp_path, failure, monkeypatch
):
    graph = video_graph(tmp_path)
    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2))
    service.embedding_client = AsyncMock()
    service.multimodal_embedder = AsyncMock()
    monkeypatch.setattr(
        "hyperrag.single_layer_kg.resolve_segment_video_path",
        lambda **kwargs: "clip.mp4",
    )
    if failure == "missing":
        graph.segments = []
    elif failure == "duplicate":
        graph.chunks[1].segment_ids = ["seg-0"]
    else:
        for i, segment in enumerate(graph.segments):
            clip = tmp_path / f"{i}.mp4"
            clip.write_bytes(b"clip")
            segment["extra"]["caption_metadata"] = {"clip_path": str(clip)}
        service.multimodal_embedder.embed_video.return_value = [1.0]
        if failure == "endpoint":
            service.multimodal_embedder.embed_video.side_effect = RuntimeError("endpoint failed")
    with pytest.raises((ValueError, RuntimeError)):
        await service._build_chunk_vector_rows(graph)
    service.embedding_client.embed_texts.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_document_preserves_vector_order_and_text_batch(tmp_path, monkeypatch):
    graph = video_graph(tmp_path)
    graph.modality = "document"
    text_chunk = ChunkRecord(
        chunk_id="text",
        file_path=graph.file_path,
        order=1,
        raw_content="manual",
        text="manual",
        tokens=1,
    )
    graph.chunks.insert(1, text_chunk)
    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2))
    service.embedding_client = AsyncMock()
    service.embedding_client.embed_texts.return_value = [[0.5, 0.5]]
    service.multimodal_embedder = AsyncMock()
    service.multimodal_embedder.embed_video.side_effect = [[1.0, 0.0], [0.0, 1.0]]
    monkeypatch.setattr(
        "hyperrag.single_layer_kg.resolve_segment_video_path",
        lambda **kwargs: f"{kwargs['segment_id']}.mp4",
    )
    rows = await service._build_chunk_vector_rows(graph)
    assert [row["item_id"] for row in rows] == ["chunk-0", "text", "chunk-1"]
    assert [row["embedding"] for row in rows] == [[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]]
    service.embedding_client.embed_texts.assert_awaited_once_with(["manual"])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_media", [False, True])
async def test_kg_handler_reports_mixed_encoding_failure_and_asset_failure(
    tmp_path, monkeypatch, missing_media
):
    from types import SimpleNamespace

    from hyperrag.task.handler import KGIndexHandler
    from hyperrag.task.models import Task, TaskResource

    service = SingleLayerKGService(HyperRAGConfig(embedding_dim=2))
    if missing_media:

        async def index(**kwargs):
            return await service._build_chunk_vector_rows(video_graph(tmp_path))

        service.index_preparsed_document = AsyncMock(side_effect=index)
        expected_error = "No video source available for segment seg-0"
    else:
        service.index_preparsed_document = AsyncMock(
            side_effect=RuntimeError("mixed embedding failed")
        )
        expected_error = "mixed embedding failed"
    runtime = SimpleNamespace(
        config=HyperRAGConfig(),
        _get_kg_experiment=AsyncMock(return_value=service),
    )
    status_store = AsyncMock()
    monkeypatch.setattr("hyperrag.task.handler.get_asset_status_storage", lambda: status_store)
    result = await KGIndexHandler(runtime)(
        Task(
            task_id="test",
            task_type="kg_index",
            status="pending",
            workspace="ws",
            asset_id="asset",
            resource=TaskResource(),
            metadata={
                "asset_path": str(tmp_path / "video.mp4"),
                "asset": {"doc_id": "asset"},
                "segments": [{"segment_id": "seg"}],
            },
        )
    )
    assert result.status == "failed"
    assert result.error == expected_error
    assert status_store.set_status.await_args.args[2].value == "failed"
