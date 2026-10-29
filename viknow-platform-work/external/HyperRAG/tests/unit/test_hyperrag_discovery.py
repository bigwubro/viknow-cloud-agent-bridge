import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from hyperrag.hyperrag_v2 import HyperRAGV2
from hyperrag.knowledge_discovery import (
    KnowledgeDiscoveryAsset,
    KnowledgeDiscoveryLibrary,
    normalize_search_text,
)


def _library(library_id: str, title: str, overview: str) -> KnowledgeDiscoveryLibrary:
    return KnowledgeDiscoveryLibrary(
        library_id=library_id,
        title=title,
        overview=overview,
        asset_count=1,
        document_count=1,
        video_count=0,
        normalized_search_text=normalize_search_text(title, overview),
        updated_at=10.0,
    )


def _asset(
    library_id: str,
    asset_id: str,
    title: str,
    *,
    summary: str = "普通资料。",
) -> KnowledgeDiscoveryAsset:
    return KnowledgeDiscoveryAsset(
        library_id=library_id,
        asset_id=asset_id,
        title=title,
        asset_type="文档",
        summary=summary,
        normalized_search_text=normalize_search_text(title, summary),
        is_active=True,
        status_updated_at=9.0,
        updated_at=10.0,
    )


class _FakeCatalog:
    def __init__(self) -> None:
        self.asset_search_calls: list[tuple[str, str | None, int]] = []
        self.libraries = {
            "library-title": _library("library-title", "维护知识库", "设备操作说明"),
            "library-asset": _library("library-asset", "工艺知识库", "常规资料"),
        }
        self.assets = [
            _asset("library-title", "asset-title", "维护计划"),
            _asset(
                "library-asset",
                "asset-asset",
                "刻蚀机维护手册",
                summary="刻蚀机维护流程。",
            ),
            _asset("library-asset", "asset-next", "维护故障记录"),
        ]

    @asynccontextmanager
    async def library_transaction(self, library_id):
        self.db = object()
        yield self

    async def list_libraries(self):
        return list(self.libraries.values())

    async def search_libraries(self, query: str, *, limit: int, library_ids=None):
        normalized = normalize_search_text(query)
        return [
            library
            for library in self.libraries.values()
            if normalized in library.normalized_search_text
            and (library_ids is None or library.library_id in library_ids)
        ][:limit]

    async def search_assets(self, query: str, *, library_id: str | None = None, limit: int):
        self.asset_search_calls.append((query, library_id, limit))
        normalized = normalize_search_text(query)
        return [
            asset
            for asset in self.assets
            if (library_id is None or asset.library_id == library_id)
            and normalized in asset.normalized_search_text
        ][:limit]

    async def search_asset_library_ids(self, query: str, *, limit: int, library_ids=None):
        return list(
            dict.fromkeys(
                asset.library_id
                for asset in await self.search_assets(query, library_id=None, limit=100)
                if library_ids is None or asset.library_id in library_ids
            )
        )[:limit]

    async def get_library(self, library_id: str):
        return self.libraries.get(library_id)


def _hyperrag(catalog: _FakeCatalog) -> HyperRAGV2:
    hyperrag = object.__new__(HyperRAGV2)
    hyperrag._knowledge_discovery_catalog_storage = catalog
    return hyperrag


@pytest.mark.asyncio
async def test_adiscover_library_matches_returns_serializable_grouped_rows() -> None:
    result = await _hyperrag(_FakeCatalog()).adiscover_library_matches(
        "刻蚀机维护", top_k=5, asset_limit=1
    )

    assert result == {
        "query": "刻蚀机维护",
        "library_matches": [
            {
                "library": {
                    "library_id": "library-asset",
                    "title": "工艺知识库",
                    "overview": "常规资料",
                    "asset_count": 1,
                    "document_count": 1,
                    "video_count": 0,
                    "normalized_search_text": "工艺知识库 常规资料",
                    "updated_at": 10.0,
                },
                "asset_matches": [
                    {
                        "library_id": "library-asset",
                        "asset_id": "asset-asset",
                        "title": "刻蚀机维护手册",
                        "type": "文档",
                        "summary": "刻蚀机维护流程。",
                        "normalized_search_text": "刻蚀机维护手册 刻蚀机维护流程",
                        "is_active": True,
                        "status_updated_at": 9.0,
                        "updated_at": 10.0,
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_adiscover_library_matches_respects_explicit_library_scope() -> None:
    result = await _hyperrag(_FakeCatalog()).adiscover_library_matches(
        "刻蚀机维护",
        library_ids=["library-title", "library-asset"],
        top_k=5,
    )

    assert [item["library"]["library_id"] for item in result["library_matches"]] == [
        "library-asset"
    ]


@pytest.mark.asyncio
async def test_adiscover_library_matches_lists_all_libraries_without_query_or_asset_lookups() -> (
    None
):
    catalog = _FakeCatalog()

    result = await _hyperrag(catalog).adiscover_library_matches("")

    assert [item["library"]["library_id"] for item in result["library_matches"]] == [
        "library-title",
        "library-asset",
    ]
    assert all(item["asset_matches"] == [] for item in result["library_matches"])
    assert catalog.asset_search_calls == []


@pytest.mark.asyncio
async def test_adiscover_library_matches_only_applies_top_k_when_explicitly_requested() -> None:
    catalog = _FakeCatalog()

    result = await _hyperrag(catalog).adiscover_library_matches("", top_k=1)

    assert [item["library"]["library_id"] for item in result["library_matches"]] == [
        "library-title"
    ]
    assert catalog.asset_search_calls == []


@pytest.mark.asyncio
async def test_adiscover_library_matches_bounds_catalog_query_concurrency_across_requests() -> None:
    class LargeCatalog:
        def __init__(self) -> None:
            self.libraries = {
                f"library-{index}": _library(
                    f"library-{index}",
                    f"知识库 {index}",
                    "设备操作资料",
                )
                for index in range(20)
            }
            self.active_asset_searches = 0
            self.max_active_asset_searches = 0
            self.asset_search_count = 0

        async def search_libraries(self, query: str, *, limit: int | None):
            del query
            return list(self.libraries.values())[:limit]

        async def search_asset_library_ids(self, query: str, *, limit: int | None):
            del query, limit
            return []

        async def get_library(self, library_id: str):
            return self.libraries.get(library_id)

        async def search_assets(self, query: str, *, library_id: str, limit: int):
            del query, library_id, limit
            self.asset_search_count += 1
            self.active_asset_searches += 1
            self.max_active_asset_searches = max(
                self.max_active_asset_searches,
                self.active_asset_searches,
            )
            await asyncio.sleep(0.01)
            self.active_asset_searches -= 1
            return []

    catalog = LargeCatalog()
    hyperrag = _hyperrag(catalog)
    hyperrag.config = SimpleNamespace(
        knowledge_discover_catalog_query_concurrency=3,
        knowledge_discover_asset_match_library_limit=5,
    )

    first_result, second_result = await asyncio.gather(
        hyperrag.adiscover_library_matches("知识库列表"),
        hyperrag.adiscover_library_matches("知识库列表"),
    )

    assert len(first_result["library_matches"]) == 20
    assert len(second_result["library_matches"]) == 20
    assert catalog.asset_search_count == 10
    assert catalog.max_active_asset_searches <= 3


@pytest.mark.asyncio
async def test_adiscover_library_matches_waits_for_catalog_backfill() -> None:
    catalog = _FakeCatalog()
    hyperrag = _hyperrag(catalog)
    hyperrag._knowledge_discovery_backfill_ready = asyncio.Event()

    async def mark_ready() -> None:
        await asyncio.sleep(0)
        hyperrag._knowledge_discovery_backfill_ready.set()

    ready_task = asyncio.create_task(mark_ready())
    result = await hyperrag.adiscover_library_matches("刻蚀机维护")
    await ready_task

    assert result["library_matches"][0]["library"]["library_id"] == "library-asset"


@pytest.mark.asyncio
async def test_adelete_knowledgebase_info_removes_its_discovery_catalog() -> None:
    catalog = _FakeCatalog()
    hyperrag = _hyperrag(catalog)
    knowledgebase_storage = SimpleNamespace(delete=AsyncMock())
    knowledgebase_storage.with_db = lambda db: knowledgebase_storage
    catalog.delete_library = AsyncMock()

    with patch(
        "hyperrag.hyperrag_v2.get_knowledgebase_info_storage",
        return_value=knowledgebase_storage,
    ):
        await hyperrag.adelete_knowledgebase_info("library-asset")

    knowledgebase_storage.delete.assert_awaited_once_with("library-asset")
    catalog.delete_library.assert_awaited_once_with("library-asset")


@pytest.mark.asyncio
async def test_failed_backfill_is_not_a_successful_empty_discovery() -> None:
    runtime = _hyperrag(_FakeCatalog())
    runtime._knowledge_discovery_backfill_ready = asyncio.Event()
    info = SimpleNamespace(get_all=AsyncMock(side_effect=RuntimeError("source unavailable")))
    with patch("hyperrag.hyperrag_v2.get_knowledgebase_info_storage", return_value=info):
        await runtime._backfill_knowledge_discovery_catalog()
    assert runtime._knowledge_discovery_backfill_ready.is_set()
    with pytest.raises(RuntimeError, match="catalog backfill failed"):
        await runtime.adiscover_library_matches("刻蚀")


@pytest.mark.asyncio
async def test_unchanged_backfill_skips_library_transactions() -> None:
    from hyperrag.knowledgebase_info import KnowledgebaseInfo

    record = KnowledgebaseInfo(
        id="kb",
        created_at=1.0,
        updated_at=20.0,
        name="guide",
        asset_ids=[],
        metadata={"overview": "maintenance"},
    )
    library = _library("kb", "guide", "maintenance")
    from dataclasses import replace

    library = replace(library, asset_count=0, document_count=0)
    catalog = SimpleNamespace(
        list_libraries=AsyncMock(return_value=[library]),
        list_all_assets=AsyncMock(return_value=[]),
        library_transaction=AsyncMock(),
    )
    runtime = _hyperrag(catalog)
    runtime._knowledge_discovery_backfill_ready = asyncio.Event()
    info = SimpleNamespace(get_all=AsyncMock(return_value=[record]))
    with patch("hyperrag.hyperrag_v2.get_knowledgebase_info_storage", return_value=info):
        await runtime._backfill_knowledge_discovery_catalog()
    await runtime._wait_for_knowledge_discovery_catalog()
    catalog.library_transaction.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["???", "的 在 和"])
async def test_nonempty_unsearchable_query_never_enumerates(query):
    catalog = _FakeCatalog()
    catalog.list_libraries = AsyncMock()
    with pytest.raises(ValueError, match="searchable"):
        await _hyperrag(catalog).adiscover_library_matches(query)
    catalog.list_libraries.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_schema_initialization_failure_never_waits_for_backfill(monkeypatch):
    runtime = object.__new__(HyperRAGV2)
    runtime._core_initialized = False
    runtime._runtime_init_lock = asyncio.Lock()
    runtime._knowledge_discovery_catalog_storage = None
    runtime._knowledge_discovery_backfill_ready = asyncio.Event()
    runtime._configure_storage_env = lambda: None
    source = SimpleNamespace(initialize=AsyncMock())
    catalog = SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError("migration required")))
    monkeypatch.setattr("hyperrag.hyperrag_v2.get_knowledgebase_info_storage", lambda: source)
    monkeypatch.setattr(
        "hyperrag.hyperrag_v2.get_knowledge_discovery_catalog_storage", lambda: catalog
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="migration"):
            await asyncio.wait_for(runtime.adiscover_library_matches("刻蚀"), timeout=0.2)
