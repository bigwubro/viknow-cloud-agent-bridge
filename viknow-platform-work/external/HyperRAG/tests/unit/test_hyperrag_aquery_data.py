from unittest.mock import AsyncMock, Mock

import pytest
from hyperrag import HyperRAGConfig, HyperRAGV2


class _DummyObservation:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def update_trace(self, metadata=None):
        self.metadata = metadata or {}

    def update(self, **kwargs):
        self.payload = kwargs


class _DummyLangfuseClient:
    def __init__(self) -> None:
        self.last_observation = None

    def start_as_current_observation(self, **kwargs):
        self.last_observation = _DummyObservation()
        return self.last_observation


@pytest.mark.asyncio
async def test_aquery_data_passes_chunk_top_k_to_vector_query(monkeypatch):
    config = HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace")
    hyperrag = HyperRAGV2(config)

    captured = {}
    langfuse_client = _DummyLangfuseClient()

    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: langfuse_client)

    async def fake_query_mix_compatible(workspace, query, *, chunk_top_k=None):
        captured["workspace"] = workspace
        captured["query"] = query
        captured["chunk_top_k"] = chunk_top_k
        return {"data": {"chunks": []}, "langfuse_trace": {}}

    hyperrag._query_mix_compatible = fake_query_mix_compatible
    hyperrag.aget_chunks_by_ids = AsyncMock(return_value={})

    result = await hyperrag.aquery_data(
        workspace="test-workspace",
        query="进入电梯顶部的步骤",
        retrieval_mode="vector",
        chunk_top_k=12,
        max_total_tokens=3456,
    )

    assert result["data"]["chunks"] == []
    assert captured["workspace"] == "test-workspace"
    assert captured["query"] == "进入电梯顶部的步骤"
    assert captured["chunk_top_k"] == 12
    payload = langfuse_client.last_observation.payload["output"]
    assert "direct_vector_chunk_ids" in payload
    assert "旧检索路径返回的文本块id" not in payload


@pytest.mark.asyncio
async def test_aquery_data_federates_union_graph_and_expands_chunks(monkeypatch):
    config = HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace")
    hyperrag = HyperRAGV2(config)
    langfuse_client = _DummyLangfuseClient()
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: langfuse_client)

    async def fake_query_mix_compatible(workspace, query, *, chunk_top_k=None):
        return {
            "status": "success",
            "data": {
                "chunks": [
                    {
                        "chunk_id": "c-direct",
                        "content": "direct chunk",
                        "metadata": {},
                    }
                ],
                "entities": [
                    {
                        "entity_name": "ViKnow",
                        "entity_type": "Product",
                        "description": "",
                        "source_id": "c-direct",
                        "file_path": "",
                        "weight": 0.9,
                    }
                ],
                "relationships": [],
                "references": [],
            },
            "langfuse_trace": {
                "chunk_ids": ["c-direct"],
                "chunks_with_ids": ["c-direct|direct chunk"],
            },
        }

    async def fake_query_vkr_delta_graph(*, workspace, query, top_k, entity_top_k):
        return {
            "chunks": [],
            "entities": [
                {
                    "entity_name": "联邦知识",
                    "entity_type": "Concept",
                    "description": "",
                    "source_id": "c-union",
                    "file_path": "",
                    "weight": 1.0,
                }
            ],
            "relationships": [
                {
                    "src_id": "联邦知识",
                    "tgt_id": "ViKnow",
                    "description": "补充关系",
                    "weight": 1.0,
                    "source_id": "c-union",
                    "file_path": "",
                    "keywords": [],
                    "situation": "",
                }
            ],
            "stats": {"matched_relations": 1},
        }

    hyperrag._query_mix_compatible = fake_query_mix_compatible
    hyperrag._query_vkr_delta_graph = fake_query_vkr_delta_graph
    hyperrag.aget_chunks_by_ids = AsyncMock(
        return_value={
            "c-union": {
                "chunk_id": "c-union",
                "content": "union expanded chunk",
                "metadata": {},
            }
        }
    )

    result = await hyperrag.aquery_data(
        workspace="test-workspace",
        query="联邦查询测试",
        retrieval_mode="vector",
        rerank_enabled=False,
    )

    chunk_ids = [chunk.get("chunk_id") for chunk in result["data"]["chunks"]]
    assert chunk_ids == ["c-direct", "c-union"]
    assert any(entity.get("entity_name") == "联邦知识" for entity in result["data"]["entities"])
    assert any(rel.get("description") == "补充关系" for rel in result["data"]["relationships"])
    payload = langfuse_client.last_observation.payload["output"]
    assert payload["federated_union_chunk_ids"] == ["c-union"]


@pytest.mark.asyncio
async def test_aquery_data_hybrid_falls_back_to_bm25_when_vector_query_fails(monkeypatch):
    config = HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace")
    hyperrag = HyperRAGV2(config)

    async def raise_vector_error(*args, **kwargs):
        raise RuntimeError("vector endpoint unavailable")

    bm25_storage = AsyncMock()
    bm25_storage.query_bm25.return_value = [("chunk-1", 0.9)]
    manager = Mock()
    manager.get_storage = AsyncMock(return_value=bm25_storage)

    hyperrag._query_mix_compatible = raise_vector_error
    hyperrag._ensure_bm25_runtime = AsyncMock()
    hyperrag.bm25_storage_manager = manager
    hyperrag.aget_chunks_by_ids = AsyncMock(
        return_value={
            "chunk-1": {
                "chunk_id": "chunk-1",
                "content": "刻蚀腔体维护流程",
                "metadata": {},
            }
        }
    )
    hyperrag._query_vkr_delta_graph = AsyncMock(
        return_value={"chunks": [], "entities": [], "relationships": [], "stats": {}}
    )

    result = await hyperrag.aquery_data(
        workspace="000531161130023253",
        query="刻蚀腔体",
        retrieval_mode="hybrid",
        rerank_enabled=False,
    )

    assert result["data"]["chunks"]
    assert result["data"]["chunks"][0]["chunk_id"] == "chunk-1"


def test_runtime_exposes_only_unified_retrieval():
    hyperrag = HyperRAGV2(HyperRAGConfig())
    assert not hasattr(hyperrag, "aquery_video_chunks")
    assert not hasattr(hyperrag, "_get_video_vdb")


@pytest.mark.asyncio
async def test_aquery_data_by_image_uses_mixed_embedding_and_returns_chunks():
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        image_retrieval_top_k=3,
    )
    hyperrag = HyperRAGV2(config)
    embedder = AsyncMock()
    embedder.embed_image_text.return_value = [0.1, 0.2]
    hyperrag.multimodal_embedder = embedder
    vector_store = AsyncMock()
    vector_store.query_chunks.return_value = [
        {
            "item_id": "chunk-1",
            "file_path": "/tmp/manual.pdf",
            "content": "阻门器说明",
            "metadata": {"chunk_id": "chunk-1"},
        }
    ]
    experiment = Mock()
    experiment._get_vector_store.return_value = vector_store
    hyperrag._ensure_core_runtime = AsyncMock()
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)
    hyperrag._get_workspace_asset_status_cache = AsyncMock(return_value={})
    hyperrag._normalize_kg_chunk_row = AsyncMock(
        return_value={
            "chunk_id": "chunk-1",
            "content": "阻门器说明",
            "file_path": "/tmp/manual.pdf",
            "metadata": {},
        }
    )

    result = await hyperrag.aquery_data_by_image(
        workspace="test-workspace",
        image_url="data:image/png;base64,ZmFrZQ==",
        query_text="阻门器",
        mix_search=True,
        chunk_top_k=3,
    )

    embedder.embed_image_text.assert_awaited_once()
    vector_store.query_chunks.assert_awaited_once_with(
        query_vector=[0.1, 0.2],
        workspace="test-workspace",
        top_k=3,
        min_similarity=-1.0,
    )
    assert result["data"]["chunks"][0]["chunk_id"] == "chunk-1"
    assert result["metadata"]["retrieval_mode"] == "image_text_mixed"


@pytest.mark.asyncio
async def test_aquery_data_by_image_uses_image_path_top_k_without_rerank():
    config = HyperRAGConfig(
        working_dir="./test-storage",
        workspace="test-workspace",
        image_retrieval_top_k=6,
    )
    hyperrag = HyperRAGV2(config)
    embedder = AsyncMock()
    embedder.embed_image_text.return_value = [0.1, 0.2]
    hyperrag.multimodal_embedder = embedder
    vector_store = AsyncMock()
    vector_store.query_chunks.return_value = [
        {
            "item_id": "chunk-1",
            "file_path": "/tmp/manual.pdf",
            "content": "阻门器说明",
            "metadata": {"chunk_id": "chunk-1"},
        }
    ]
    experiment = Mock()
    experiment._get_vector_store.return_value = vector_store
    hyperrag._ensure_core_runtime = AsyncMock()
    hyperrag._get_kg_experiment = AsyncMock(return_value=experiment)
    hyperrag._get_workspace_asset_status_cache = AsyncMock(return_value={})
    hyperrag._normalize_kg_chunk_row = AsyncMock(
        return_value={
            "chunk_id": "chunk-1",
            "content": "阻门器说明",
            "file_path": "/tmp/manual.pdf",
            "metadata": {},
        }
    )
    hyperrag._rerank_query_chunks = AsyncMock(return_value=[])

    await hyperrag.aquery_data_by_image(
        workspace="test-workspace",
        image_url="data:image/png;base64,ZmFrZQ==",
        query_text="阻门器",
        mix_search=True,
    )

    vector_store.query_chunks.assert_awaited_once_with(
        query_vector=[0.1, 0.2],
        workspace="test-workspace",
        top_k=6,
        min_similarity=-1.0,
    )
    hyperrag._rerank_query_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_aquery_data_by_image_rejects_non_base64_image_url():
    hyperrag = HyperRAGV2(HyperRAGConfig(working_dir="./test-storage", workspace="test-workspace"))

    with pytest.raises(ValueError, match="data:image"):
        await hyperrag.aquery_data_by_image(
            workspace="test-workspace",
            image_url="https://example.test/image.png",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid", "bm25"])
async def test_video_is_returned_from_unified_retrieval_in_each_mode(monkeypatch, mode):
    runtime = HyperRAGV2(HyperRAGConfig())
    video = {
        "chunk_id": "canonical-video",
        "content": "action",
        "file_path": "/tmp/video.mp4",
        "metadata": {
            "chunk_type": "video",
            "asset_id": "asset",
            "segment_ids": ["segment"],
            "begin_ts": 1,
            "end_ts": 2,
            "claims_by_segment": [{"segment_id": "segment"}],
        },
    }
    result = {
        "status": "success",
        "data": {"chunks": [video], "entities": [], "relationships": []},
    }
    unified = AsyncMock(return_value=result)
    runtime._query_mix_compatible = unified
    runtime._query_hybrid = unified
    runtime._query_bm25_only = unified
    runtime._query_vkr_delta_graph = AsyncMock(return_value={})
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", lambda: _DummyLangfuseClient())
    # Any accidental reintroduction of a second recall path must fail the test.
    runtime.aquery_video_chunks = AsyncMock(side_effect=AssertionError("separate video recall"))
    response = await runtime.aquery_data("ws", "action", retrieval_mode=mode, rerank_enabled=False)
    assert response["data"]["chunks"] == [video]
    assert response["data"]["references"]
    unified.assert_awaited_once()
    runtime.aquery_video_chunks.assert_not_awaited()
