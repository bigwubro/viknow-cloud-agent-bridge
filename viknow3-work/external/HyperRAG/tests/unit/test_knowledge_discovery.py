from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from typing import Any

import hyperrag.knowledge_discovery as discovery_module
import pytest
from hyperrag.knowledge_discovery import (
    SQL_TEMPLATES_KNOWLEDGE_DISCOVERY,
    KnowledgeDiscoveryAsset,
    KnowledgeDiscoveryCatalogStorage,
    KnowledgeDiscoveryLibrary,
    normalize_search_text,
)
from hyperrag.runtime_primitives import initialize_share_data


class _FakeDB:
    def __init__(self) -> None:
        self.libraries: dict[str, dict[str, Any]] = {}
        self.assets: dict[tuple[str, str], dict[str, Any]] = {}
        self.executed: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy((self.libraries, self.assets))
        try:
            yield self
        except BaseException:
            self.libraries, self.assets = before
            raise

    async def execute(self, sql: str, params: list[Any] | None = None, **_kwargs: Any) -> None:
        self.executed.append(sql)
        params = params or []
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["upsert_library"]:
            self.libraries[params[0]] = {
                "library_id": params[0],
                "title": params[1],
                "overview": params[2],
                "asset_count": params[3],
                "document_count": params[4],
                "video_count": params[5],
                "normalized_search_text": params[6],
                "updated_at": params[7],
                "search_tokens": params[8],
                "search_version": params[9],
            }
        elif sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["upsert_asset"]:
            self.assets[(params[0], params[1])] = {
                "library_id": params[0],
                "asset_id": params[1],
                "title": params[2],
                "asset_type": params[3],
                "summary": params[4],
                "normalized_search_text": params[5],
                "is_active": params[6],
                "status_updated_at": params[7],
                "updated_at": params[8],
                "search_tokens": params[9],
                "search_version": params[10],
            }
        elif sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["deactivate_missing_assets"]:
            library_id, active_ids, updated_at = params
            for (candidate_library_id, asset_id), row in self.assets.items():
                if candidate_library_id == library_id and asset_id not in active_ids:
                    row["is_active"] = False
                    row["updated_at"] = updated_at
        elif sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["delete_library"]:
            self.libraries.pop(params[0], None)
            self.assets = {key: row for key, row in self.assets.items() if key[0] != params[0]}
        elif sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["delete_asset"]:
            self.assets.pop((params[0], params[1]), None)

    async def query(self, sql: str, params: list[Any], *, multirows: bool = False) -> Any:
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["get_library"]:
            return self.libraries.get(params[0])
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_libraries"]:
            return list(self.libraries.values())
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["get_asset"]:
            return self.assets.get((params[0], params[1]))
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_assets"]:
            return [row for key, row in self.assets.items() if key[0] == params[0]]
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["list_all_assets"]:
            return list(self.assets.values())
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_libraries"]:
            query, tokens, patterns, limit, library_ids = params
            return [
                row
                for row in self.libraries.values()
                if (library_ids is None or row["library_id"] in library_ids)
                and (
                    set(tokens).intersection(row["search_tokens"])
                    or any(
                        pattern.strip("%") in row["normalized_search_text"] for pattern in patterns
                    )
                )
            ][: limit or None]
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_assets"]:
            query, tokens, patterns, limit, library_id = params
            return [
                row
                for row in self.assets.values()
                if row["is_active"]
                and (library_id is None or row["library_id"] == library_id)
                and (
                    set(tokens).intersection(row["search_tokens"])
                    or any(
                        pattern.strip("%") in row["normalized_search_text"] for pattern in patterns
                    )
                )
            ][:limit]
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["search_asset_library_ids"]:
            query, tokens, patterns, limit, library_ids = params
            matching_library_ids = {
                row["library_id"]
                for row in self.assets.values()
                if row["is_active"]
                and (library_ids is None or row["library_id"] in library_ids)
                and (
                    set(tokens).intersection(row["search_tokens"])
                    or any(
                        pattern.strip("%") in row["normalized_search_text"] for pattern in patterns
                    )
                )
            }
            return [
                {"library_id": library_id}
                for library_id in sorted(matching_library_ids)[: limit or None]
            ]
        return [] if multirows else None


class _FakeClientManager:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db

    async def get_client(self) -> _FakeDB:
        return self.db

    async def release_client(self, _db: _FakeDB) -> None:
        return None


@pytest.fixture
def catalog_storage(monkeypatch: pytest.MonkeyPatch) -> KnowledgeDiscoveryCatalogStorage:
    fake_db = _FakeDB()
    monkeypatch.setattr(discovery_module, "ClientManager", _FakeClientManager(fake_db))
    initialize_share_data(workers=1)
    return KnowledgeDiscoveryCatalogStorage(
        namespace="knowledge_discovery_catalog",
        workspace="",
        global_config={},
    )


def _library(
    library_id: str = "kb-1",
    *,
    title: str = "设备维护库",
    asset_count: int = 2,
) -> KnowledgeDiscoveryLibrary:
    return KnowledgeDiscoveryLibrary(
        library_id=library_id,
        title=title,
        overview="包含刻蚀机维护流程和故障排查。",
        asset_count=asset_count,
        document_count=asset_count,
        video_count=0,
        normalized_search_text=normalize_search_text(title, "包含刻蚀机维护流程和故障排查。"),
        updated_at=100.0,
    )


def _asset(
    asset_id: str,
    *,
    title: str,
    summary: str,
    updated_at: float = 100.0,
) -> KnowledgeDiscoveryAsset:
    return KnowledgeDiscoveryAsset(
        library_id="kb-1",
        asset_id=asset_id,
        title=title,
        asset_type="文档",
        summary=summary,
        normalized_search_text=normalize_search_text(title, "文档", summary),
        is_active=True,
        status_updated_at=90.0,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_catalog_synchronizes_snapshot_and_marks_deleted_assets_inactive(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    first_assets = [
        _asset("asset-1", title="刻蚀机维护手册", summary="描述清洗步骤。"),
        _asset("asset-2", title="故障排查记录", summary="描述真空故障。"),
    ]
    await catalog_storage.synchronize_library(_library(), first_assets)
    await catalog_storage.synchronize_library(
        _library(asset_count=1),
        [_asset("asset-2", title="故障排查记录", summary="描述真空故障。")],
    )

    deleted = await catalog_storage.get_asset("kb-1", "asset-1")
    active = await catalog_storage.get_asset("kb-1", "asset-2")
    assert deleted is not None and not deleted.is_active
    assert active is not None and active.is_active
    assert (await catalog_storage.get_library("kb-1")).asset_count == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_catalog_search_prefers_title_matches_without_embeddings(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    await catalog_storage.synchronize_library(
        _library(),
        [
            _asset("summary-hit", title="其他资料", summary="刻蚀机维护说明", updated_at=300.0),
            _asset("title-hit", title="刻蚀机维护手册", summary="例行操作", updated_at=100.0),
        ],
    )

    results = await catalog_storage.search_assets("刻蚀机维护", library_id="kb-1")

    assert [result.asset_id for result in results] == ["title-hit", "summary-hit"]
    assert "hyperrag_knowledge_discovery_libraries" in "\n".join(
        catalog_storage.db.executed  # type: ignore[union-attr]
    )
    assert "embedding" not in "\n".join(catalog_storage.db.executed).lower()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_catalog_search_matches_terms_from_a_longer_question(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    await catalog_storage.synchronize_library(
        _library(),
        [_asset("ema-clean", title="EMA wet clean SOP", summary="湿法清洗步骤。")],
    )

    results = await catalog_storage.search_assets(
        "EMA机台的wet clean要怎么做",
        library_id="kb-1",
    )

    assert [result.asset_id for result in results] == ["ema-clean"]


@pytest.mark.asyncio
async def test_catalog_search_matches_chinese_concepts_from_a_broader_question(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    await catalog_storage.synchronize_library(
        _library(),
        [_asset("etch-process", title="刻蚀工艺手册", summary="介绍各类刻蚀工艺。")],
    )

    results = await catalog_storage.search_assets(
        "刻蚀有哪些工艺",
        library_id="kb-1",
    )

    assert [result.asset_id for result in results] == ["etch-process"]


@pytest.mark.asyncio
async def test_catalog_distinct_asset_libraries_prevents_one_library_from_crowding_results(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    await catalog_storage.synchronize_library(
        _library("kb-1"),
        [
            _asset("etch-1", title="刻蚀工艺一", summary="刻蚀工艺"),
            _asset("etch-2", title="刻蚀工艺二", summary="刻蚀工艺"),
        ],
    )
    await catalog_storage.synchronize_library(
        _library("kb-2", title="第二知识库"),
        [
            KnowledgeDiscoveryAsset(
                library_id="kb-2",
                asset_id="etch-3",
                title="刻蚀工艺三",
                asset_type="文档",
                summary="刻蚀工艺",
                normalized_search_text=normalize_search_text("刻蚀工艺三", "刻蚀工艺"),
                is_active=True,
                status_updated_at=90.0,
                updated_at=100.0,
            )
        ],
    )

    assert await catalog_storage.search_asset_library_ids("刻蚀工艺", limit=2) == ["kb-1", "kb-2"]


@pytest.mark.asyncio
async def test_catalog_search_accepts_an_unlimited_result_request(
    catalog_storage: KnowledgeDiscoveryCatalogStorage,
) -> None:
    await catalog_storage.synchronize_library(_library("kb-1"), [])
    await catalog_storage.synchronize_library(_library("kb-2"), [])

    results = await catalog_storage.search_libraries("设备", limit=None)

    assert [result.library_id for result in results] == ["kb-1", "kb-2"]


def test_normalize_search_text_is_case_and_whitespace_stable() -> None:
    assert normalize_search_text(" LAM-2300 ", "维护\n流程") == "lam 2300 维护 流程"


@pytest.mark.asyncio
async def test_invalid_snapshot_leaves_no_partial_library(catalog_storage) -> None:
    asset = _asset("asset-1", title="guide", summary="maintenance")
    with pytest.raises(ValueError, match="unique"):
        await catalog_storage.synchronize_library(_library(), [asset, asset])
    assert await catalog_storage.get_library("kb-1") is None


@pytest.mark.asyncio
async def test_failed_snapshot_rolls_back_library_and_assets(catalog_storage, monkeypatch) -> None:
    await catalog_storage.initialize()
    original = catalog_storage.db.execute

    async def fail_asset(sql, params=None, **kwargs):
        if sql == SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["upsert_asset"]:
            raise RuntimeError("injected asset failure")
        return await original(sql, params, **kwargs)

    monkeypatch.setattr(catalog_storage.db, "execute", fail_asset)
    with pytest.raises(RuntimeError, match="injected"):
        await catalog_storage.synchronize_library(
            _library(), [_asset("a", title="guide", summary="maintenance")]
        )
    assert await catalog_storage.get_library("kb-1") is None


def test_snapshot_content_ignores_sync_clock_but_not_business_changes() -> None:
    from hyperrag.knowledge_discovery import catalog_content

    asset = _asset("a", title="guide", summary="maintenance")
    assert catalog_content(asset) == catalog_content(replace(asset, updated_at=999.0))
    assert catalog_content(asset) != catalog_content(replace(asset, summary="changed"))
    assert catalog_content(asset) != catalog_content(replace(asset, is_active=False))
    assert catalog_content(asset) != catalog_content(replace(asset, status_updated_at=None))


def test_catalog_sql_updates_only_changed_content() -> None:
    for name in ("upsert_library", "upsert_asset"):
        sql = SQL_TEMPLATES_KNOWLEDGE_DISCOVERY[name]
        assert "IS DISTINCT FROM" in sql
        assert "updated_at) IS DISTINCT" not in sql
    assert "AND is_active = TRUE" in SQL_TEMPLATES_KNOWLEDGE_DISCOVERY["deactivate_missing_assets"]
