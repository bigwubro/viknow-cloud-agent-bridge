import datetime
import json
from typing import Any, Dict, List, Optional

import pytest

import hyperrag.asset_status as asset_status
from hyperrag.runtime_primitives import initialize_share_data
from hyperrag.asset_status import AssetStatus, AssetStatusStorage, SQL_TEMPLATES_ASSET_STATUS


class _FakeDB:
    def __init__(self):
        self._rows: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def execute(self, sql: str, params: Optional[Dict[str, Any]] = None, **_kwargs) -> None:
        params = params or {}
        if sql == SQL_TEMPLATES_ASSET_STATUS["upsert"]:
            workspace = params["workspace"]
            record = dict(params)
            self._rows.setdefault(workspace, {})[params["id"]] = record
            return
        if sql == SQL_TEMPLATES_ASSET_STATUS["update_metadata"]:
            workspace = params["$1"]
            asset_id = params["$2"]
            record = self._rows.setdefault(workspace, {}).get(asset_id)
            if record is not None:
                current = record.get("metadata")
                current = json.loads(current) if isinstance(current, str) else current
                merged = {
                    **(current if isinstance(current, dict) else {}),
                    **json.loads(params["$3"]),
                }
                for key in params.get("$5") or []:
                    merged.pop(key, None)
                record["metadata"] = json.dumps(merged)
                record["updated_at"] = params["$4"]
            return
        if sql == SQL_TEMPLATES_ASSET_STATUS["delete"]:
            workspace = params["workspace"]
            ids = params.get("ids") or []
            for doc_id in ids:
                self._rows.get(workspace, {}).pop(doc_id, None)
            return
        if sql == SQL_TEMPLATES_ASSET_STATUS["drop"]:
            workspace = params["workspace"]
            self._rows.pop(workspace, None)
            return
        # create table / migration / index creation: ignore
        return

    async def query(self, sql: str, params: List[Any], multirows: bool = False):
        workspace = params[0] if params else ""
        if sql == SQL_TEMPLATES_ASSET_STATUS["get_by_id"]:
            asset_id = params[1]
            row = self._rows.get(workspace, {}).get(asset_id)
            return dict(row) if row else None
        if sql == SQL_TEMPLATES_ASSET_STATUS["get_by_ids"]:
            ids = params[1] or []
            rows = [self._rows.get(workspace, {}).get(aid) for aid in ids]
            return [r for r in rows if r] if multirows else rows
        if sql == SQL_TEMPLATES_ASSET_STATUS["get_all"]:
            rows = list(self._rows.get(workspace, {}).values())
            return rows if multirows else rows[0] if rows else None
        if sql == SQL_TEMPLATES_ASSET_STATUS["filter_keys"]:
            ids = params[1] or []
            existing = [aid for aid in ids if aid in self._rows.get(workspace, {})]
            return [{"id": aid} for aid in existing] if multirows else None
        if sql == SQL_TEMPLATES_ASSET_STATUS["is_empty"]:
            has_data = bool(self._rows.get(workspace))
            return {"has_data": has_data}
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
    monkeypatch.setattr(asset_status, "ClientManager", fake_manager)
    initialize_share_data(workers=1)
    storage = AssetStatusStorage(
        namespace="asset_status",
        workspace="",
        global_config={},
        embedding_func=None,
    )
    yield storage


@pytest.mark.asyncio
async def test_asset_status_set_and_get(fake_storage):
    storage = fake_storage
    await storage.initialize()
    workspace = "ws_unit"
    await storage.set_status(workspace, "a1", AssetStatus.QUEUED, progress=0.1)
    status = await storage.get_status(workspace, "a1")
    assert status is not None
    assert status.asset_id == "a1"
    assert status.status == AssetStatus.QUEUED


@pytest.mark.asyncio
async def test_asset_status_update_metadata_merges_incrementally(fake_storage):
    storage = fake_storage
    await storage.initialize()
    workspace = "ws_metadata"
    await storage.set_status(
        workspace,
        "a1",
        AssetStatus.PARSING,
        metadata={"asset_type": "doc"},
    )

    await storage.update_metadata(workspace, "a1", {"page_count": 12})

    status = await storage.get_status(workspace, "a1")
    assert status is not None
    assert status.metadata == {"asset_type": "doc", "page_count": 12}

    await storage.update_metadata(
        workspace,
        "a1",
        {"chunk_nums": 37},
        remove_keys=["doc_type"],
    )
    status = await storage.get_status(workspace, "a1")
    assert status is not None
    assert status.metadata == {
        "asset_type": "doc",
        "page_count": 12,
        "chunk_nums": 37,
    }


@pytest.mark.asyncio
async def test_asset_status_rejects_undefined_status(fake_storage):
    await fake_storage.initialize()

    with pytest.raises(ValueError, match="Unsupported asset status: 'delete'"):
        await fake_storage.set_status("ws_unit", "a1", "delete")

    with pytest.raises(ValueError, match="Unsupported asset status: 'delete'"):
        await fake_storage.upsert("ws_unit", {"a1": {"status": "delete"}})

    assert await fake_storage.get_status("ws_unit", "a1") is None


@pytest.mark.asyncio
async def test_asset_status_upsert_and_list(fake_storage):
    storage = fake_storage
    await storage.initialize()
    workspace = "ws_bulk"
    now = datetime.datetime.utcnow()
    payload = {
        "a1": {
            "asset_id": "a1",
            "status": AssetStatus.QUEUED.value,
            "created_at": now,
            "updated_at": now,
            "progress": 0.0,
        },
        "a2": {
            "asset_id": "a2",
            "status": AssetStatus.PARSING.value,
            "created_at": now,
            "updated_at": now,
            "progress": 0.2,
        },
    }
    await storage.upsert(workspace, payload)
    all_status = await storage.get_all_status(workspace)
    assert len(all_status) == 2
    parsing = sum(1 for s in all_status.values() if s.status == AssetStatus.PARSING)
    assert parsing == 1
