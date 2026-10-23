from typing import Any, Dict, List, Optional

import pytest

import hyperrag.knowledgebase_info as knowledgebase_info_module
from hyperrag.knowledgebase_info import (
    KnowledgebaseInfoStorage,
    SQL_TEMPLATES_KNOWLEDGEBASE_INFO,
)
from hyperrag.runtime_primitives import initialize_share_data


class _FakeDB:
    def __init__(self):
        self._rows: Dict[str, Dict[str, Any]] = {}

    async def execute(self, sql: str, params: Optional[Dict[str, Any]] = None, **_kwargs) -> None:
        params = params or {}
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["upsert"]:
            self._rows[params["id"]] = dict(params)
            return
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["delete"]:
            self._rows.pop(params["id"], None)
            return
        return

    async def query(self, sql: str, params: List[Any], multirows: bool = False):
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_by_id"]:
            return self._rows.get(params[0])
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_by_ids"]:
            ids = params[0] or []
            rows = [self._rows.get(kb_id) for kb_id in ids]
            return [row for row in rows if row] if multirows else (rows[0] if rows else None)
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_all"]:
            rows = list(self._rows.values())
            return rows if multirows else (rows[0] if rows else None)
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["filter_keys"]:
            ids = params[0] or []
            rows = [{"id": kb_id} for kb_id in ids if kb_id in self._rows]
            return rows if multirows else (rows[0] if rows else None)
        if sql == SQL_TEMPLATES_KNOWLEDGEBASE_INFO["is_empty"]:
            return {"has_data": bool(self._rows)}
        return [] if multirows else None


class _FakeClientManager:
    def __init__(self, db: _FakeDB):
        self._db = db

    async def get_client(self):
        return self._db

    async def release_client(self, _db):
        return None


@pytest.fixture
def fake_storage(monkeypatch):
    fake_db = _FakeDB()
    fake_manager = _FakeClientManager(fake_db)
    monkeypatch.setattr(knowledgebase_info_module, "ClientManager", fake_manager)
    initialize_share_data(workers=1)
    storage = KnowledgebaseInfoStorage(
        namespace="knowledgebase_info",
        workspace="",
        global_config={},
        embedding_func=None,
    )
    yield storage


@pytest.mark.asyncio
async def test_knowledgebase_info_upsert_and_get(fake_storage):
    storage = fake_storage
    await storage.initialize()

    saved = await storage.upsert(
        "kb_demo",
        name="示例知识库",
        asset_ids=["a1", "a2", "a1"],
        summarization="当前知识库已索引完成2个文档、0个视频。",
        metadata={"description": "demo"},
    )

    loaded = await storage.get("kb_demo")

    assert saved.id == "kb_demo"
    assert loaded is not None
    assert loaded.name == "示例知识库"
    assert loaded.asset_ids == ["a1", "a2"]
    assert loaded.summarization.startswith("当前知识库已索引完成")
    assert loaded.metadata == {"description": "demo"}


@pytest.mark.asyncio
async def test_knowledgebase_info_upsert_merges_existing_fields(fake_storage):
    storage = fake_storage
    await storage.initialize()

    original = await storage.upsert(
        "kb_demo",
        name="初始名称",
        asset_ids=["a1"],
        summarization="初始摘要",
        metadata={"description": "v1"},
    )
    updated = await storage.upsert("kb_demo", asset_ids=["a1", "a2"])

    assert updated.name == "初始名称"
    assert updated.asset_ids == ["a1", "a2"]
    assert updated.summarization == "初始摘要"
    assert updated.metadata == {"description": "v1"}
    assert updated.created_at == pytest.approx(original.created_at, abs=1e-6)


@pytest.mark.asyncio
async def test_knowledgebase_info_delete(fake_storage):
    storage = fake_storage
    await storage.initialize()
    await storage.upsert("kb_demo", name="demo")

    await storage.delete("kb_demo")

    assert await storage.get("kb_demo") is None
