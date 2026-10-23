from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from hyperrag.fast_ingest_runtime import (
    PENDING_LINT,
    UNVERIFIED,
    VERIFIED,
    FastIngestRelation,
    FastIngestRuntime,
)
from hyperrag.hyperrag_v2 import HyperRAGV2


class _FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    async def hget(self, key: str, field: str):
        return self._hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str):
        self._hashes.setdefault(key, {})[field] = value
        return 1

    async def hgetall(self, key: str):
        return dict(self._hashes.get(key, {}))

    async def hdel(self, key: str, field: str):
        if key not in self._hashes:
            return 0
        if field in self._hashes[key]:
            del self._hashes[key][field]
            return 1
        return 0

    async def sadd(self, key: str, value: str):
        self._sets.setdefault(key, set()).add(value)
        return 1

    async def smembers(self, key: str):
        return set(self._sets.get(key, set()))

    async def srem(self, key: str, value: str):
        if key not in self._sets:
            return 0
        if value in self._sets[key]:
            self._sets[key].remove(value)
            return 1
        return 0

    async def delete(self, key: str):
        deleted = 0
        if key in self._hashes:
            del self._hashes[key]
            deleted += 1
        if key in self._sets:
            del self._sets[key]
            deleted += 1
        return deleted


@pytest.mark.asyncio
async def test_ingest_relations_absorbs_duplicates():
    runtime = FastIngestRuntime(_FakeRedis())
    edges = [
        FastIngestRelation(
            source_entity="ViKnow",
            target_entity="HyperRAG",
            relation_type="集成",
            relation_description="ViKnow 集成 HyperRAG",
            chunk_id="c1",
        ),
        FastIngestRelation(
            source_entity="ViKnow",
            target_entity="HyperRAG",
            relation_type="集成",
            relation_description="ViKnow 集成 HyperRAG",
            chunk_id="c2",
        ),
    ]
    stats = await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=edges,
        compile_phase="FAST_INGEST",
    )
    assert stats["created_edges"] == 1
    assert stats["absorbed_edges"] == 1
    assert stats["duplicate_absorb_rate"] == 0.5
    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    assert len(edge_map) == 1
    payload = json.loads(next(iter(edge_map.values())))
    assert payload["doc_id"] == "doc-1"
    assert int(payload["occurrence"]) == 2
    assert sorted(payload["source_refs"]) == ["doc-1:c1", "doc-1:c2"]


@pytest.mark.asyncio
async def test_post_compile_retains_hot_buffer_when_graph_merge_is_incomplete():
    class FakeRuntime:
        def __init__(self) -> None:
            self.finalized: list[bool] = []

        async def prepare_post_compile_batch(self, *, workspace: str, batch_size: int):
            return {"batch_id": "batch-1", "edges": [{"edge_id": "edge-1"}]}

        async def finish_post_compile_batch(self, *, workspace: str, batch_id: str, success: bool):
            self.finalized.append(success)
            return {"removed_edges": 0}

    class FakeRepository:
        async def merge_fast_ingest_post_compile_edges(self, **_kwargs):
            return {"merged_edges": 0}

    class FakeExperiment:
        repository = FakeRepository()

        async def upsert_fast_ingest_post_compile_vectors(self, _edges):
            raise AssertionError("vectors must not be written before graph persistence succeeds")

    runtime = FakeRuntime()
    app = object.__new__(HyperRAGV2)
    app.config = SimpleNamespace(
        post_compile_batch_size=10,
        fast_ingest_compile_phase="FAST_INGEST",
    )

    async def ensure_fast_ingest_runtime():
        return runtime

    async def get_kg_experiment(_workspace: str):
        return FakeExperiment()

    app._ensure_fast_ingest_runtime = ensure_fast_ingest_runtime
    app._get_kg_experiment = get_kg_experiment

    with pytest.raises(RuntimeError, match="did not persist every selected edge"):
        await app.apost_compile_batch(workspace="w1")

    assert runtime.finalized == [False]


@pytest.mark.asyncio
async def test_query_delta_chunks_returns_matched_rows():
    runtime = FastIngestRuntime(_FakeRedis())
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[
            FastIngestRelation(
                source_entity="Qwen-2.5-7B-Instruct",
                target_entity="vLLM",
                relation_type="部署在",
                relation_description="模型部署在 vLLM 推理服务",
                chunk_id="c1",
            )
        ],
        compile_phase="FAST_INGEST",
    )
    chunks = await runtime.query_delta_chunks(workspace="w1", query="Qwen vLLM", top_k=5)
    assert len(chunks) == 1
    assert chunks[0]["metadata"]["vkr_source"] == "delta_hot_buffer"
    graph_hits = await runtime.query_delta_graph(
        workspace="w1",
        query="Qwen vLLM",
        top_k_relations=5,
        top_k_entities=5,
    )
    assert len(graph_hits["relationships"]) == 1
    assert graph_hits["relationships"][0]["src_id"] == "Qwen-2.5-7B-Instruct"
    assert len(graph_hits["entities"]) == 2


@pytest.mark.asyncio
async def test_post_compile_and_lint_state_transitions():
    runtime = FastIngestRuntime(_FakeRedis())
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[
            FastIngestRelation(
                source_entity="A",
                target_entity="B",
                relation_type="关联",
                relation_description="A 关联 B",
                chunk_id="c1",
            )
        ],
        compile_phase="FAST_INGEST",
    )
    prepared = await runtime.prepare_post_compile_batch(workspace="w1", batch_size=10)
    assert prepared["batch_id"] != ""
    mark = await runtime.mark_batch_pending_lint(workspace="w1", batch_id=prepared["batch_id"])
    assert mark["updated"] == 1

    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    one = next(iter(edge_map.values()))
    assert PENDING_LINT in one

    lint = await runtime.lint_promote(workspace="w1", max_edges=10)
    assert lint["selected"] == 1

    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    one = next(iter(edge_map.values()))
    assert VERIFIED in one
    assert UNVERIFIED not in one


@pytest.mark.asyncio
async def test_post_compile_keeps_relations_ingested_during_batch_for_the_next_batch():
    runtime = FastIngestRuntime(_FakeRedis())
    first = FastIngestRelation(
        source_entity="A",
        target_entity="B",
        relation_type="关联",
        relation_description="A 关联 B",
        chunk_id="c1",
    )
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[first],
    )
    prepared = await runtime.prepare_post_compile_batch(workspace="w1", batch_size=10)
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[
            FastIngestRelation(
                source_entity="A",
                target_entity="B",
                relation_type="关联",
                relation_description="A 关联 B",
                chunk_id="c2",
            )
        ],
    )

    finalized = await runtime.finish_post_compile_batch(
        workspace="w1",
        batch_id=prepared["batch_id"],
        success=True,
    )

    assert finalized["removed_edges"] == 0
    assert finalized["restored_edges"] == 1
    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    payload = json.loads(next(iter(edge_map.values())))
    assert payload["status"] == UNVERIFIED
    assert payload["source_refs"] == ["doc-1:c2"]


@pytest.mark.asyncio
async def test_delete_doc_relations_removes_asset_edges_from_hot_buffer():
    runtime = FastIngestRuntime(_FakeRedis())
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[
            FastIngestRelation(
                source_entity="A",
                target_entity="B",
                relation_type="关联",
                relation_description="A 关联 B",
                chunk_id="c1",
            )
        ],
        compile_phase="FAST_INGEST",
    )

    deleted = await runtime.delete_doc_relations(workspace="w1", doc_id="doc-1")
    assert deleted["deleted_edges"] == 1

    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    assert edge_map == {}
    doc_edges = await runtime.redis.smembers(runtime._doc_edges_key("w1", "doc-1"))
    assert doc_edges == set()


@pytest.mark.asyncio
async def test_delete_doc_relations_keeps_shared_edges_of_other_docs():
    runtime = FastIngestRuntime(_FakeRedis())
    relation = FastIngestRelation(
        source_entity="A",
        target_entity="B",
        relation_type="关联",
        relation_description="A 关联 B",
        chunk_id="c1",
    )
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[relation],
        compile_phase="FAST_INGEST",
    )
    await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-2",
        relations=[relation],
        compile_phase="FAST_INGEST",
    )

    deleted = await runtime.delete_doc_relations(workspace="w1", doc_id="doc-1")
    assert deleted["updated_edges"] == 0
    assert deleted["deleted_edges"] == 1

    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    assert len(edge_map) == 1
    payload = json.loads(next(iter(edge_map.values())))
    assert payload["source_refs"] == ["doc-2:c1"]


@pytest.mark.asyncio
async def test_ingest_relations_does_not_merge_across_docs():
    runtime = FastIngestRuntime(_FakeRedis())
    relation = FastIngestRelation(
        source_entity="ViKnow",
        target_entity="HyperRAG",
        relation_type="集成",
        relation_description="ViKnow 集成 HyperRAG",
        chunk_id="c1",
    )
    stats_doc1 = await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-1",
        relations=[relation],
        compile_phase="FAST_INGEST",
    )
    stats_doc2 = await runtime.ingest_relations(
        workspace="w1",
        doc_id="doc-2",
        relations=[relation],
        compile_phase="FAST_INGEST",
    )

    assert stats_doc1["created_edges"] == 1
    assert stats_doc2["created_edges"] == 1
    assert stats_doc2["absorbed_edges"] == 0

    edge_map = await runtime.redis.hgetall(runtime._edge_map_key("w1"))
    assert len(edge_map) == 2
