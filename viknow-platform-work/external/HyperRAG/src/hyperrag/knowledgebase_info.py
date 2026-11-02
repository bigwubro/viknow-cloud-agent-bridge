from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, final

from .runtime_primitives import BaseKVStorage, ClientManager, PostgreSQLDB, get_data_init_lock

logger = logging.getLogger(__name__)


@dataclass
class KnowledgebaseInfo:
    id: str
    created_at: float
    updated_at: float
    name: str | None = None
    asset_ids: list[str] = field(default_factory=list)
    summarization: str = ""
    metadata: dict[str, Any] | None = None


SQL_TEMPLATES_KNOWLEDGEBASE_INFO = {
    "create_table": """
        CREATE TABLE IF NOT EXISTS hyperrag_knowledgebase_info (
            id VARCHAR(255) PRIMARY KEY,
            name TEXT,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            asset_ids JSONB,
            summarization TEXT,
            metadata JSONB
        )
    """,
    "create_indexes": [
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_knowledgebase_info_updated_at ON hyperrag_knowledgebase_info(updated_at DESC)",
    ],
    "upsert": """
        INSERT INTO hyperrag_knowledgebase_info
        (id, name, created_at, updated_at, asset_ids, summarization, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            updated_at = EXCLUDED.updated_at,
            asset_ids = EXCLUDED.asset_ids,
            summarization = EXCLUDED.summarization,
            metadata = EXCLUDED.metadata
    """,
    "get_by_id": """
        SELECT * FROM hyperrag_knowledgebase_info
        WHERE id=$1
    """,
    "get_by_ids": """
        SELECT * FROM hyperrag_knowledgebase_info
        WHERE id = ANY($1)
    """,
    "get_all": """
        SELECT * FROM hyperrag_knowledgebase_info
        ORDER BY updated_at DESC
    """,
    "filter_keys": """
        SELECT id FROM hyperrag_knowledgebase_info
        WHERE id = ANY($1)
    """,
    "delete": """
        DELETE FROM hyperrag_knowledgebase_info
        WHERE id=$1
    """,
    "drop": """
        DELETE FROM hyperrag_knowledgebase_info
    """,
    "is_empty": """
        SELECT EXISTS(SELECT 1 FROM hyperrag_knowledgebase_info LIMIT 1) as has_data
    """,
}


@final
@dataclass
class KnowledgebaseInfoStorage(BaseKVStorage):
    db: PostgreSQLDB | None = field(default=None)

    def __post_init__(self):
        self._initialized = False

    def with_db(self, db: PostgreSQLDB) -> KnowledgebaseInfoStorage:
        storage = replace(self, db=db)
        storage._initialized = True
        return storage

    async def initialize(self):
        if self._initialized:
            return

        async with get_data_init_lock():
            if self.db is None:
                if ClientManager is None:
                    raise RuntimeError(
                        "PostgreSQL backend requires HyperRAG database configuration. "
                        "Please ensure PostgreSQL is properly configured."
                    )
                self.db = await ClientManager.get_client()

            await self.db.execute(SQL_TEMPLATES_KNOWLEDGEBASE_INFO["create_table"], upsert=True)
            for index_sql in SQL_TEMPLATES_KNOWLEDGEBASE_INFO["create_indexes"]:
                await self.db.execute(index_sql, upsert=True, ignore_if_exists=True)

        self._initialized = True
        logger.info("KnowledgebaseInfoStorage initialized (global singleton)")

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def ensure_initialize(self):
        if not self._initialized:
            await self.initialize()

    def _parse_datetime(self, dt_input):
        if dt_input is None:
            return None
        if isinstance(dt_input, (int, float)):
            dt = datetime.datetime.fromtimestamp(dt_input, tz=datetime.UTC)
            return dt.replace(tzinfo=None)
        if isinstance(dt_input, datetime.datetime):
            if dt_input.tzinfo is None:
                dt_input = dt_input.replace(tzinfo=datetime.UTC)
            return dt_input.astimezone(datetime.UTC).replace(tzinfo=None)
        try:
            dt = datetime.datetime.fromisoformat(str(dt_input))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt.astimezone(datetime.UTC).replace(tzinfo=None)
        except (ValueError, TypeError):
            logger.warning(f"Unable to parse datetime: {dt_input}")
            return None

    def _normalize_asset_ids(self, asset_ids: Any) -> list[str]:
        if asset_ids is None:
            return []
        if isinstance(asset_ids, str):
            try:
                asset_ids = json.loads(asset_ids)
            except Exception:
                return []
        if not isinstance(asset_ids, list):
            return []
        out: list[str] = []
        seen = set()
        for item in asset_ids:
            value = str(item).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _normalize_metadata(self, metadata: Any) -> dict[str, Any] | None:
        if metadata is None:
            return None
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                return None
        return metadata if isinstance(metadata, dict) else None

    def _datetime_to_timestamp(self, value: Any) -> float:
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.UTC)
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        return time.time()

    def _to_record(self, row: dict[str, Any]) -> KnowledgebaseInfo:
        return KnowledgebaseInfo(
            id=row["id"],
            name=row.get("name"),
            created_at=self._datetime_to_timestamp(row.get("created_at")),
            updated_at=self._datetime_to_timestamp(row.get("updated_at")),
            asset_ids=self._normalize_asset_ids(row.get("asset_ids")),
            summarization=str(row.get("summarization") or ""),
            metadata=self._normalize_metadata(row.get("metadata")),
        )

    async def get_by_id(self, knowledgebase_id: str) -> dict[str, Any] | None:
        await self.ensure_initialize()
        return await self.db.query(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_by_id"],
            [knowledgebase_id],
        )

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        await self.ensure_initialize()
        if not ids:
            return []
        results = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_by_ids"],
            [ids],
            multirows=True,
        )
        return results or []

    async def get(self, knowledgebase_id: str) -> KnowledgebaseInfo | None:
        result = await self.get_by_id(knowledgebase_id)
        if not result:
            return None
        return self._to_record(result)

    async def get_all(self) -> list[KnowledgebaseInfo]:
        await self.ensure_initialize()
        results = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["get_all"],
            [],
            multirows=True,
        )
        if not results:
            return []
        return [self._to_record(row) for row in results]

    async def upsert(
        self,
        knowledgebase_id: str,
        *,
        name: str | None = None,
        asset_ids: list[str] | None = None,
        summarization: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgebaseInfo:
        await self.ensure_initialize()

        existing = await self.get(knowledgebase_id)
        created_at = existing.created_at if existing is not None else time.time()
        updated_at = time.time()
        merged = KnowledgebaseInfo(
            id=knowledgebase_id,
            name=name if name is not None else (existing.name if existing else None),
            created_at=created_at,
            updated_at=updated_at,
            asset_ids=self._normalize_asset_ids(
                asset_ids if asset_ids is not None else (existing.asset_ids if existing else [])
            ),
            summarization=(
                str(summarization)
                if summarization is not None
                else (existing.summarization if existing else "")
            ),
            metadata=metadata
            if metadata is not None
            else (existing.metadata if existing else None),
        )
        if (
            existing is not None
            and replace(
                merged,
                updated_at=existing.updated_at,
            )
            == existing
        ):
            return existing

        await self.db.execute(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["upsert"],
            {
                "id": merged.id,
                "name": merged.name,
                "created_at": self._parse_datetime(merged.created_at),
                "updated_at": self._parse_datetime(merged.updated_at),
                "asset_ids": json.dumps(merged.asset_ids),
                "summarization": merged.summarization,
                "metadata": json.dumps(merged.metadata) if merged.metadata is not None else None,
            },
        )
        return merged

    async def delete(self, knowledgebase_id: str) -> None:
        await self.ensure_initialize()
        await self.db.execute(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["delete"],
            {"id": knowledgebase_id},
        )

    async def drop(self) -> dict[str, str]:
        await self.ensure_initialize()
        await self.db.execute(SQL_TEMPLATES_KNOWLEDGEBASE_INFO["drop"], None)
        return {"status": "success", "message": "data dropped"}

    async def filter_keys(self, keys: set[str]) -> set[str]:
        await self.ensure_initialize()
        if not keys:
            return set()
        results = await self.db.query(
            SQL_TEMPLATES_KNOWLEDGEBASE_INFO["filter_keys"],
            [list(keys)],
            multirows=True,
        )
        existing = {row["id"] for row in (results or [])}
        return {key for key in keys if key not in existing}

    async def is_empty(self) -> bool:
        await self.ensure_initialize()
        result = await self.db.query(SQL_TEMPLATES_KNOWLEDGEBASE_INFO["is_empty"], [])
        return not result.get("has_data", False) if result else True

    async def index_done_callback(self) -> None:
        pass


_knowledgebase_info_storage: KnowledgebaseInfoStorage | None = None


def get_knowledgebase_info_storage() -> KnowledgebaseInfoStorage:
    global _knowledgebase_info_storage
    if _knowledgebase_info_storage is None:
        if PostgreSQLDB is None:
            raise RuntimeError(
                "PostgreSQL backend is not available. "
                "Please ensure the HyperRAG PostgreSQL dependencies are properly installed."
            )
        _knowledgebase_info_storage = KnowledgebaseInfoStorage(
            namespace="knowledgebase_info",
            workspace="",
            global_config={},
            embedding_func=None,
        )
    return _knowledgebase_info_storage
