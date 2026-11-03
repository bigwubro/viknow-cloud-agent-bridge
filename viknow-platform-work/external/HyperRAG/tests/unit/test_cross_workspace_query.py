import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from hyperrag.bm25_storage_manager import BM25StorageManager
from hyperrag.bm25_vdb import PGBM25Storage
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.kg_vector_store import KGVectorStore
from hyperrag.mix_query import SingleModeMixQueryEngine
from hyperrag.single_layer_kg import SingleLayerKGService


def vector_store():
    db = SimpleNamespace(query=AsyncMock(return_value=[]))
    store = KGVectorStore("a", "public", 2, "chunks", "entities", "relations", db=db)
    store._initialized = True
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chunks", "entities", "relations"])
async def test_vector_range_ranks_once_with_scope_before_limit(kind):
    store = vector_store()
    await store.query_cross_workspace(
        kind=kind, query_vector=[1.0, 0.0], workspaces=["a", "b"], top_k=1
    )
    sql, args = store.db.query.call_args.args
    assert "SELECT workspace, item_id" in sql
    assert "workspace = ANY($2)" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert args[1] == ["a", "b"]
    assert store.db.query.await_count == 1


@pytest.mark.asyncio
async def test_bm25_range_retains_compound_identity():
    db = SimpleNamespace(
        query=AsyncMock(
            return_value=[
                {"workspace": "a", "id": "same", "bm25_score": -2.0},
                {"workspace": "b", "id": "same", "bm25_score": -1.0},
            ]
        )
    )
    store = PGBM25Storage({}, "hyperrag", "a", db=db)
    rows = await store.query_bm25_cross_workspace("Si HBr", workspaces=["a", "b"], top_k=2)
    assert [(row["workspace"], row["id"]) for row in rows] == [("a", "same"), ("b", "same")]
    sql, args = db.query.call_args.args
    assert "workspace = ANY($2)" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert args == ["Si HBr", ["a", "b"], 2]


@pytest.mark.asyncio
async def test_compound_chunk_fetch_preserves_pairs_and_workspace():
    store = vector_store()
    await store.get_chunks_by_keys(chunk_keys=[("a", "same"), ("b", "other")])
    sql, args = store.db.query.call_args.args
    assert "SELECT workspace, item_id" in sql
    assert "(workspace, item_id) IN" in sql
    assert "unnest($1::text[], $2::text[])" in sql
    assert args == [["a", "b"], ["same", "other"]]


@pytest.mark.asyncio
async def test_empty_storage_ranges_do_not_access_database():
    store = vector_store()
    assert (
        await store.query_cross_workspace(kind="chunks", query_vector=[1, 0], workspaces=[]) == []
    )
    assert await store.get_chunks_by_keys(chunk_keys=[]) == []
    store.db.query.assert_not_awaited()
    bm25 = PGBM25Storage({}, "hyperrag", "a", db=store.db)
    assert await bm25.query_bm25_cross_workspace("Si", workspaces=[]) == []
    store.db.query.assert_not_awaited()


def source_row(workspace, item_id, *, metadata=None, similarity=0.8):
    return {
        "workspace": workspace,
        "item_id": item_id,
        "file_path": "same.pdf",
        "content": f"{workspace}:{item_id}",
        "similarity": similarity,
        "metadata": {"doc_id": "asset", "tokens": 12, **(metadata or {})},
    }


@pytest.fixture
def cross_runtime(monkeypatch):
    rows = {
        "chunks": [source_row(ws, "same") for ws in ("a", "b")],
        "entities": [
            source_row(
                ws, "entity", metadata={"entity_name": "Shared name", "chunk_ids": ["graph"]}
            )
            for ws in ("a", "b")
        ],
        "relations": [
            source_row(
                ws,
                "relation",
                metadata={
                    "source_entity": "Shared name",
                    "target_entity": "Target",
                    "relationship_description": "supported relation",
                    "chunk_ids": ["relation-source"],
                },
            )
            for ws in ("a", "b")
        ],
    }

    async def query_vectors(*, kind, workspaces, **kwargs):
        return [row for row in rows[kind] if row["workspace"] in workspaces]

    async def fetch(*, chunk_keys):
        return [source_row(ws, item_id) for ws, item_id in chunk_keys]

    async def synonyms(*, workspace, **kwargs):
        return [
            {
                **source_row(
                    workspace,
                    "alias",
                    metadata={
                        "entity_name": "Shared name",
                        "normalized_name": "shared name",
                        "chunk_ids": ["graph"],
                    },
                ),
                "file_path": "alias.pdf",
            }
        ]

    store = SimpleNamespace(
        query_cross_workspace=AsyncMock(side_effect=query_vectors),
        get_chunks_by_keys=AsyncMock(side_effect=fetch),
        find_entity_synonyms=AsyncMock(side_effect=synonyms),
    )
    embedding = SimpleNamespace(embed_texts=AsyncMock(return_value=[[1.0, 0.0]]))
    writer = AsyncMock()
    engine = SingleModeMixQueryEngine(
        workspace="a",
        vector_store=store,
        embedding_client=embedding,
        synonym_relation_writer=writer,
    )
    service = SingleLayerKGService.__new__(SingleLayerKGService)
    service._vector_enabled = lambda: True
    service._get_mix_query_engine = lambda: engine
    service._get_vector_store = lambda: store

    async def query_bm25(query, *, workspaces, **kwargs):
        return [{"workspace": ws, "id": "bm", "bm25_score": -1.0} for ws in workspaces]

    runtime = HyperRAGV2.__new__(HyperRAGV2)
    runtime.config = SimpleNamespace(
        precompile_workspace="private-precompile",
        bm25_storage_backend="postgres",
        kg_vector_chunk_top_k=20,
        kg_vector_entity_top_k=10,
        kg_vector_relation_top_k=12,
        rerank_enabled=False,
        rerank_top_k=50,
        rerank_score_threshold=0.55,
    )
    catalog = SimpleNamespace(
        list_libraries=AsyncMock(
            return_value=[
                SimpleNamespace(library_id=ws)
                for ws in ("a", "b", "PRECOMPILED", "private-precompile")
            ]
        )
    )
    runtime._get_knowledge_discovery_catalog_storage = AsyncMock(return_value=catalog)
    runtime._get_kg_experiment = AsyncMock(return_value=service)
    runtime._ensure_bm25_runtime = AsyncMock()
    runtime.bm25_storage_manager = SimpleNamespace(
        query_cross_workspace=AsyncMock(side_effect=query_bm25)
    )
    runtime._jieba_tokenize = Mock(side_effect=lambda text, **kwargs: f"tokenized:{text}")
    runtime._get_workspace_asset_status_cache = AsyncMock(return_value={})
    runtime.aquery_video_chunks = AsyncMock(side_effect=AssertionError("No cross visual search"))
    runtime._query_vkr_delta_graph = AsyncMock(side_effect=AssertionError("No cross delta"))
    runtime._query_mix_compatible = AsyncMock(
        side_effect=AssertionError("No per-library retrieval")
    )
    runtime._query_hybrid = AsyncMock(side_effect=AssertionError("No per-library hybrid"))
    return SimpleNamespace(
        runtime=runtime,
        service=service,
        store=store,
        rows=rows,
        embedding=embedding,
        writer=writer,
        catalog=catalog,
        engine=engine,
    )


@pytest.mark.asyncio
async def test_failed_directory_readiness_cannot_authorize_cross_workspace(cross_runtime):
    runtime = cross_runtime.runtime
    runtime._knowledge_discovery_backfill_ready = asyncio.Event()
    runtime._knowledge_discovery_backfill_ready.set()
    runtime._knowledge_discovery_backfill_error = RuntimeError("source failed")
    with pytest.raises(RuntimeError, match="catalog backfill failed"):
        await runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["Si"])
    cross_runtime.catalog.list_libraries.assert_not_awaited()
    cross_runtime.embedding.embed_texts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "library_ids,expected",
    [
        (None, {"a", "b"}),
        ([], set()),
        (["a"], {"a"}),
        (["b", "a", "missing", "PRECOMPILED", "private-precompile"], {"a", "b"}),
        (["missing", "PRECOMPILED"], set()),
    ],
)
async def test_one_authorized_range_and_independent_lexical_input(
    cross_runtime, library_ids, expected
):
    f = cross_runtime
    result = await f.runtime.aquery_data_cross_workspace(
        query="semantic expansion",
        bm25_keywords=["Si+HBr=", "original"],
        library_ids=library_ids,
    )
    if not expected:
        assert result["reason"] == "no_accessible_libraries"
        f.embedding.embed_texts.assert_not_awaited()
        f.store.query_cross_workspace.assert_not_awaited()
        f.runtime._ensure_bm25_runtime.assert_not_awaited()
        if library_ids == []:
            f.runtime._get_knowledge_discovery_catalog_storage.assert_not_awaited()
        return
    assert result["status"] == "success"
    f.embedding.embed_texts.assert_awaited_once_with(["semantic expansion"])
    assert f.store.query_cross_workspace.await_count == 3
    assert {call.kwargs["kind"] for call in f.store.query_cross_workspace.await_args_list} == {
        "chunks",
        "entities",
        "relations",
    }
    for call in f.store.query_cross_workspace.await_args_list:
        assert set(call.kwargs["workspaces"]) == expected
    lexical_call = f.runtime.bm25_storage_manager.query_cross_workspace.await_args
    assert lexical_call.args == ("tokenized:Si+HBr= original",)
    assert set(lexical_call.kwargs["workspaces"]) == expected
    f.runtime._jieba_tokenize.assert_called_once_with("Si+HBr= original", strict=True)
    assert f.runtime._get_kg_experiment.await_count == 1
    assert {c["workspace"] for c in result["data"]["chunks"]} == expected
    f.writer.assert_not_awaited()
    f.runtime.aquery_video_chunks.assert_not_awaited()
    f.runtime._query_vkr_delta_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_ids_keep_graph_bm25_and_reference_sources_separate(cross_runtime):
    f = cross_runtime
    result = await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])
    data = result["data"]
    chunks = {(c["workspace"], c["chunk_id"]): c for c in data["chunks"]}
    assert set(chunks) == {
        (ws, cid) for ws in ("a", "b") for cid in ("same", "graph", "relation-source", "bm")
    }
    for (ws, cid), chunk in chunks.items():
        assert chunk["content"] == f"{ws}:{cid}"
        assert chunk["metadata"]["doc_id"] == "asset"
        if cid == "graph":
            assert "Shared name" in chunk["entity_hits"]
        if cid == "relation-source":
            assert chunk["relation_hits"] == ["Shared name -> Target"]
    assert {(e["workspace"], e["item_id"]) for e in data["entities"]} == {
        (ws, item_id) for ws in ("a", "b") for item_id in ("entity", "alias")
    }
    assert {(r["workspace"], r["relation_id"]) for r in data["relationships"]} == {
        ("a", "relation"),
        ("b", "relation"),
    }
    assert all(r["source_id"] == "relation-source" for r in data["relationships"])
    assert {(r["workspace"], r["file_path"]) for r in data["references"]} == {
        ("a", "same.pdf"),
        ("b", "same.pdf"),
    }
    assert len({c["reference_id"] for c in chunks.values()}) == 2
    assert {call.kwargs["workspace"] for call in f.store.find_entity_synonyms.await_args_list} == {
        "a",
        "b",
    }
    source_call = f.store.get_chunks_by_keys.await_args_list[0]
    assert set(source_call.kwargs["chunk_keys"]) == {
        (ws, cid) for ws in ("a", "b") for cid in ("graph", "relation-source")
    }


def test_relation_support_cannot_cross_same_named_entity():
    engine = SingleModeMixQueryEngine(workspace="a", vector_store=None, embedding_client=None)
    relations = engine._build_relation_hits(
        [source_row("b", "relation", metadata={"source_entity": "Shared"})],
        entity_hits=[{"workspace": "a", "item_id": "e", "entity_name": "Shared", "score": 1000}],
        all_keywords=[],
    )
    assert relations[0]["score"] == 80.0


def test_rrf_keeps_same_ids_from_different_libraries():
    a = {"workspace": "a", "chunk_id": "same"}
    b = {"workspace": "b", "chunk_id": "same"}
    assert HyperRAGV2._fuse_ranked_chunks([a, b], [b, a], rrf_k=60) == [a, b]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("query", ""),
        ("query", 1),
        ("bm25_keywords", []),
        ("bm25_keywords", [" "]),
        ("bm25_keywords", "raw"),
        ("bm25_keywords", ["ok", 1]),
        ("library_ids", "a"),
        ("library_ids", [None]),
        ("chunk_top_k", 0),
        ("chunk_top_k", True),
        ("rerank_score_threshold", float("nan")),
        ("rerank_score_threshold", 1.1),
    ],
)
async def test_cross_invalid_inputs_fail_before_runtime(cross_runtime, field, value):
    kwargs = {"query": "semantic", "bm25_keywords": ["raw"], field: value}
    with pytest.raises(ValueError):
        await cross_runtime.runtime.aquery_data_cross_workspace(**kwargs)
    cross_runtime.runtime._get_knowledge_discovery_catalog_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_json_cross_is_explicitly_unsupported_even_for_empty_scope(cross_runtime):
    cross_runtime.runtime.config.bm25_storage_backend = "json"
    with pytest.raises(NotImplementedError, match="PostgreSQL"):
        await cross_runtime.runtime.aquery_data_cross_workspace(
            query="semantic", bm25_keywords=["raw"], library_ids=[]
        )
    cross_runtime.runtime._get_knowledge_discovery_catalog_storage.assert_not_awaited()
    manager = BM25StorageManager(cross_runtime.runtime.config)
    with pytest.raises(NotImplementedError):
        await manager.query_cross_workspace("raw", workspaces=["a"], top_k=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_stage",
    [
        "catalog",
        "embedding",
        "vector",
        "synonyms",
        "bm25",
        "source",
        "hydrate",
    ],
)
async def test_cross_backend_failures_are_not_success_empty(cross_runtime, failing_stage):
    f = cross_runtime
    failures = {
        "catalog": f.catalog.list_libraries,
        "embedding": f.embedding.embed_texts,
        "vector": f.store.query_cross_workspace,
        "synonyms": f.store.find_entity_synonyms,
        "bm25": f.runtime.bm25_storage_manager.query_cross_workspace,
        "source": f.store.get_chunks_by_keys,
        "hydrate": f.runtime._get_workspace_asset_status_cache,
    }
    failures[failing_stage].side_effect = RuntimeError(failing_stage)
    with pytest.raises(RuntimeError, match=failing_stage):
        await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])


@pytest.mark.asyncio
async def test_missing_graph_or_bm25_source_is_a_failure(cross_runtime):
    f = cross_runtime
    f.store.get_chunks_by_keys.side_effect = None
    f.store.get_chunks_by_keys.return_value = []
    with pytest.raises(RuntimeError, match="source chunks"):
        await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])


@pytest.mark.asyncio
async def test_nonvector_service_cannot_silently_downgrade(cross_runtime):
    cross_runtime.service._vector_enabled = lambda: False
    with pytest.raises(NotImplementedError, match="PostgreSQL"):
        await cross_runtime.service.query_cross_workspace(query="semantic", workspaces=["a", "b"])
    cross_runtime.store.query_cross_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerank_uses_semantics_and_keeps_compound_identity(cross_runtime, monkeypatch):
    f = cross_runtime
    f.runtime.config.rerank_enabled = True
    f.runtime._build_rerank_api_url = lambda: "https://unused.invalid"

    async def rerank(query, candidates):
        assert query == "semantic"
        assert len({candidate.id for candidate in candidates}) == len(candidates)
        return [
            SimpleNamespace(candidate_id=c.id, relevance_score=0.6 + i / 100)
            for i, c in enumerate(candidates)
        ]

    client = SimpleNamespace(rerank=AsyncMock(side_effect=rerank))
    factory = Mock(return_value=client)
    monkeypatch.setattr("hyperrag.rerank.OpenAICompatibleReranker", factory)
    result = await f.runtime.aquery_data_cross_workspace(
        query="semantic", bm25_keywords=["raw"], chunk_top_k=4, rerank_score_threshold=0.61
    )
    assert factory.call_args.kwargs["mode"] == "text"
    assert len(result["data"]["chunks"]) == 4
    assert all(c["metadata"]["rerank_score"] >= 0.61 for c in result["data"]["chunks"])
    assert len({(c["workspace"], c["chunk_id"]) for c in result["data"]["chunks"]}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_result", ["error", "missing", "duplicate", "nan"])
async def test_cross_rerank_failure_cannot_fall_back(cross_runtime, monkeypatch, bad_result):
    f = cross_runtime
    f.runtime.config.rerank_enabled = True
    f.runtime._build_rerank_api_url = lambda: "https://unused.invalid"

    async def rerank(query, candidates):
        if bad_result == "error":
            raise RuntimeError("rerank failure")
        if bad_result == "missing":
            return []
        if bad_result == "duplicate":
            return [SimpleNamespace(candidate_id="0", relevance_score=1)] * len(candidates)
        return [
            SimpleNamespace(candidate_id=c.id, relevance_score=float("nan")) for c in candidates
        ]

    monkeypatch.setattr(
        "hyperrag.rerank.OpenAICompatibleReranker",
        lambda **kwargs: SimpleNamespace(rerank=AsyncMock(side_effect=rerank)),
    )
    with pytest.raises(RuntimeError):
        await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])


@pytest.mark.asyncio
async def test_single_hybrid_keywords_do_not_change_semantic_query(cross_runtime):
    runtime = cross_runtime.runtime
    runtime._query_mix_compatible = AsyncMock(
        return_value={
            "status": "success",
            "data": {"chunks": []},
            "langfuse_trace": {},
        }
    )
    storage = SimpleNamespace(query_bm25=AsyncMock(return_value=[("bm", -1.0)]))
    runtime.bm25_storage_manager.get_storage = AsyncMock(return_value=storage)
    runtime.aget_chunks_by_ids = AsyncMock(
        return_value={"bm": {"chunk_id": "bm", "content": "raw"}}
    )
    result = await HyperRAGV2._query_hybrid(
        runtime,
        "a",
        "semantic expansion",
        SimpleNamespace(chunk_top_k=2),
        bm25_keywords=["Si+HBr=", "raw"],
    )
    assert result["data"]["chunks"][0]["chunk_id"] == "bm"
    runtime._query_mix_compatible.assert_awaited_once_with("a", "semantic expansion", chunk_top_k=2)
    storage.query_bm25.assert_awaited_once_with("tokenized:Si+HBr= raw", "a", 60)


@pytest.mark.asyncio
async def test_legacy_vector_none_still_means_instance_workspace():
    store = vector_store()
    await store.query_chunks(query_vector=[1, 0], workspace=None, top_k=2)
    sql, args = store.db.query.call_args.args
    assert "WHERE workspace = $2" in sql
    assert args[1] == "a"


@pytest.mark.asyncio
async def test_manager_queries_shared_bm25_once_not_each_library():
    manager = BM25StorageManager(SimpleNamespace(bm25_storage_backend="postgres"))
    storage = SimpleNamespace(query_bm25_cross_workspace=AsyncMock(return_value=[]))
    manager.get_storage = AsyncMock(return_value=storage)
    await manager.query_cross_workspace("raw", workspaces=["a", "b"], top_k=2)
    manager.get_storage.assert_awaited_once_with("a")
    storage.query_bm25_cross_workspace.assert_awaited_once_with(
        "raw", workspaces=["a", "b"], top_k=2
    )


@pytest.mark.asyncio
async def test_wrong_workspace_synonym_is_never_inferred(cross_runtime):
    f = cross_runtime
    f.store.find_entity_synonyms.side_effect = None
    f.store.find_entity_synonyms.return_value = [
        source_row("b", "alias", metadata={"entity_name": "Shared name"})
    ]
    with pytest.raises(RuntimeError, match="different workspace"):
        await f.runtime.aquery_data_cross_workspace(
            query="semantic", bm25_keywords=["raw"], library_ids=["a"]
        )
    f.writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_tokenizer_receives_original_keywords_not_semantic_query(cross_runtime):
    runtime = cross_runtime.runtime
    runtime.bm25_stopwords = set()
    runtime._jieba_tokenize = HyperRAGV2._jieba_tokenize.__get__(runtime)
    await runtime.aquery_data_cross_workspace(
        query="semantic expansion", bm25_keywords=["Si+HBr=", "ABC-123"]
    )
    args = runtime.bm25_storage_manager.query_cross_workspace.await_args.args
    assert args == ("si+hbr abc-123",)
    cross_runtime.embedding.embed_texts.assert_awaited_once_with(["semantic expansion"])


@pytest.mark.asyncio
async def test_unsearchable_or_failed_tokenization_cannot_fall_back(cross_runtime):
    runtime = cross_runtime.runtime
    runtime._jieba_tokenize = Mock(return_value="")
    with pytest.raises(ValueError, match="searchable tokens"):
        await runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["!!!"])
    cross_runtime.embedding.embed_texts.assert_not_awaited()
    runtime._jieba_tokenize = Mock(side_effect=RuntimeError("tokenizer failure"))
    with pytest.raises(RuntimeError, match="tokenizer failure"):
        await runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])
    cross_runtime.embedding.embed_texts.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_valid_rerank_scores_below_threshold_is_success_empty(cross_runtime, monkeypatch):
    f = cross_runtime
    f.runtime.config.rerank_enabled = True
    f.runtime._build_rerank_api_url = lambda: "https://unused.invalid"

    async def rerank(query, candidates):
        return [SimpleNamespace(candidate_id=c.id, relevance_score=0.1) for c in candidates]

    monkeypatch.setattr(
        "hyperrag.rerank.OpenAICompatibleReranker",
        lambda **kwargs: SimpleNamespace(rerank=AsyncMock(side_effect=rerank)),
    )
    result = await f.runtime.aquery_data_cross_workspace(
        query="semantic", bm25_keywords=["raw"], rerank_score_threshold=0.8
    )
    assert result["status"] == "success"
    assert result["data"]["chunks"] == []
    assert result["data"]["references"] == []


@pytest.mark.asyncio
async def test_cross_engine_trace_keeps_compound_ids(cross_runtime):
    result = await cross_runtime.engine.query(
        query="semantic", workspaces=["a", "b"], top_chunks=20
    )
    trace = result["langfuse_trace"]
    assert ("a", "same") in trace["chunk_ids"]
    assert ("b", "same") in trace["chunk_ids"]
    assert "a|same|a:same" in trace["chunks_with_ids"]
    assert "b|same|b:same" in trace["chunks_with_ids"]


@pytest.mark.asyncio
async def test_cross_source_fetch_rejects_bm25_only_missing_chunk(cross_runtime):
    f = cross_runtime

    async def fetch(*, chunk_keys):
        return [source_row(ws, cid) for ws, cid in chunk_keys if cid != "bm"]

    f.store.get_chunks_by_keys.side_effect = fetch
    with pytest.raises(RuntimeError, match="BM25 source chunks"):
        await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])


@pytest.mark.asyncio
async def test_cross_unauthorized_vector_row_is_failure_before_hydration(cross_runtime):
    f = cross_runtime
    f.rows["chunks"].append(source_row("private", "same"))
    f.store.query_cross_workspace.side_effect = lambda **kwargs: f.rows[kwargs["kind"]]
    with pytest.raises(RuntimeError, match="invalid workspace"):
        await f.runtime.aquery_data_cross_workspace(query="semantic", bm25_keywords=["raw"])
    f.runtime._get_workspace_asset_status_cache.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hybrid", "bm25"])
async def test_single_public_query_forwards_optional_bm25_keywords(
    cross_runtime, monkeypatch, mode
):
    runtime = cross_runtime.runtime
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_client", MagicMock())
    runtime.config.vkr_delta_top_k = 5
    empty = {"status": "success", "data": {"chunks": [], "entities": [], "relationships": []}}
    runtime._query_hybrid = AsyncMock(return_value=empty)
    runtime._query_bm25_only = AsyncMock(return_value=empty)
    runtime.aquery_video_chunks = AsyncMock(return_value=[])
    runtime._query_vkr_delta_graph = AsyncMock(return_value={})
    result = await runtime.aquery_data(
        "a", "semantic", retrieval_mode=mode, bm25_keywords=["Si+HBr=", "raw"]
    )
    assert result["status"] == "success"
    if mode == "hybrid":
        call = runtime._query_hybrid.await_args
        assert call.args[:2] == ("a", "semantic")
        assert call.kwargs == {"bm25_keywords": ["Si+HBr=", "raw"]}
    else:
        runtime._query_bm25_only.assert_awaited_once_with("a", "Si+HBr= raw")
    runtime.aquery_video_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_single_mix_retains_synonym_writeback(cross_runtime):
    f = cross_runtime
    for kind in ("chunks", "entities", "relations"):
        setattr(
            f.store,
            f"query_{kind}",
            AsyncMock(return_value=[row for row in f.rows[kind] if row["workspace"] == "a"]),
        )

    async def fetch(*, workspace, chunk_ids):
        return [source_row(workspace, item_id) for item_id in chunk_ids]

    f.store.get_chunks_by_ids = AsyncMock(side_effect=fetch)
    result = await f.engine.query(query="Shared name", top_chunks=8)
    await f.engine.close()
    assert result["query_scope"] == "workspace"
    assert {item["workspace"] for item in result["chunks"]} == {"a"}
    f.writer.assert_awaited_once()
    assert all(row["workspace"] == "a" for row in f.writer.await_args.args[0])
    assert len(f.writer.await_args.args[0]) == 2
