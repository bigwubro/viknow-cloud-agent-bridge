"""
ChunkMeta 存储：保存 chunk 的元数据信息（segment_ids 和 metadata）

⚠️  DEPRECATED: 本模块已废弃
------------------------------------
chunk_meta 数据现已迁移到 HyperRAG 的 custom_data 字段中。
segment_ids 和 metadata 现在作为 chunk 的自定义字段直接保存在 HYPERRAG_DOC_CHUNKS 表中，
不再需要单独的 HYPERRAG_CHUNK_META 表。

本文件保留仅用于：
1. 可能的数据迁移工具
2. 向后兼容性参考

新的实现方式：
- chunker 生成的 chunk 字典直接包含 segment_ids 和 metadata 字段
- HyperRAG 的 PostgreSQL 存储自动将这些字段保存到 custom_data JSONB 列
- 查询时，custom_data 字段会自动合并到 chunk 字典的顶层

设计目标（已废弃）：
- 使用 PostgreSQL 作为存储后端（参考 asset_status.py 的实现）
- 存储 chunk_id、segment_ids（JSONB）、metadata（JSONB）
- 支持批量查询和更新
"""

import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, final

from .runtime_primitives import (
    BaseKVStorage,
    ClientManager,
    PostgreSQLDB,
    get_data_init_lock,
    get_namespace_lock,
)

logger = logging.getLogger(__name__)

# SQL Templates for Chunk Meta Storage
SQL_TEMPLATES_CHUNK_META = {
    "create_table": """
        CREATE TABLE IF NOT EXISTS HYPERRAG_CHUNK_META (
            workspace VARCHAR(255) NOT NULL,
            id VARCHAR(500) NOT NULL,
            chunk_id VARCHAR(500) NOT NULL,
            segment_ids JSONB,
            metadata JSONB,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (workspace, id)
        )
    """,
    "create_indexes": [
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_chunk_meta_workspace ON HYPERRAG_CHUNK_META(workspace)",
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_chunk_meta_chunk_id ON HYPERRAG_CHUNK_META(workspace, chunk_id)",
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_chunk_meta_updated_at ON HYPERRAG_CHUNK_META(workspace, updated_at DESC)",
    ],
    "upsert": """
        INSERT INTO HYPERRAG_CHUNK_META 
        (workspace, id, chunk_id, segment_ids, metadata, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (workspace, id) DO UPDATE SET
            chunk_id = EXCLUDED.chunk_id,
            segment_ids = EXCLUDED.segment_ids,
            metadata = EXCLUDED.metadata,
            updated_at = EXCLUDED.updated_at
    """,
    "get_by_id": """
        SELECT * FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1 AND id=$2
    """,
    "get_by_ids": """
        SELECT * FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "get_all": """
        SELECT * FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1
    """,
    "filter_keys": """
        SELECT id FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "delete": """
        DELETE FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "drop": """
        DELETE FROM HYPERRAG_CHUNK_META 
        WHERE workspace=$1
    """,
    "is_empty": """
        SELECT EXISTS(SELECT 1 FROM HYPERRAG_CHUNK_META WHERE workspace=$1 LIMIT 1) as has_data
    """,
}


@final
@dataclass
class PGChunkMetaStorage(BaseKVStorage):
    """基于 PostgreSQL 的 chunk 元数据存储"""

    db: PostgreSQLDB | None = field(default=None)

    def __post_init__(self):
        self._initialized = False

    async def initialize(self):
        """初始化 PostgreSQL 存储"""
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

            # Implement workspace priority
            if self.db.workspace:
                self.workspace = self.db.workspace
            elif hasattr(self, "workspace") and self.workspace:
                pass
            else:
                self.workspace = "default"

            # Create table and indexes
            await self.db.execute(SQL_TEMPLATES_CHUNK_META["create_table"], upsert=True)
            for index_sql in SQL_TEMPLATES_CHUNK_META["create_indexes"]:
                await self.db.execute(index_sql, upsert=True, ignore_if_exists=True)

        self._initialized = True
        logger.info(f"[{self.workspace}] PGChunkMetaStorage initialized")

    async def finalize(self):
        """清理数据库连接"""
        async with get_namespace_lock(self.namespace, workspace=self.workspace):
            if self.db is not None:
                await ClientManager.release_client(self.db)
                self.db = None

    async def ensure_initialize(self):
        if not self._initialized:
            await self.initialize()

    def _parse_datetime(self, dt_input):
        """Parse datetime and ensure it's stored as UTC time in database"""
        if dt_input is None:
            return None

        # If it's a float timestamp, convert to datetime
        if isinstance(dt_input, (int, float)):
            dt = datetime.datetime.fromtimestamp(dt_input, tz=datetime.UTC)
            return dt.replace(tzinfo=None)  # Remove timezone for storage

        if isinstance(dt_input, (datetime.date, datetime.datetime)):
            if isinstance(dt_input, datetime.datetime):
                if dt_input.tzinfo is None:
                    dt_input = dt_input.replace(tzinfo=datetime.UTC)
                return dt_input.astimezone(datetime.UTC).replace(tzinfo=None)
            return dt_input

        try:
            dt = datetime.datetime.fromisoformat(str(dt_input))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt.astimezone(datetime.UTC).replace(tzinfo=None)
        except (ValueError, TypeError):
            logger.warning(f"[{self.workspace}] Unable to parse datetime: {dt_input}")
            return None

    async def get_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """获取 chunk 元数据，返回原始存储字典"""
        await self.ensure_initialize()

        sql = SQL_TEMPLATES_CHUNK_META["get_by_id"]
        params = [self.workspace, chunk_id]
        result = await self.db.query(sql, params)

        if result:
            # Convert timestamps back to float (Unix timestamp)
            result["created_at"] = (
                result["created_at"].timestamp() if result.get("created_at") else time.time()
            )
            result["updated_at"] = (
                result["updated_at"].timestamp() if result.get("updated_at") else time.time()
            )
            # Normalize segment_ids and metadata: prefer list/dict; tolerate legacy string payloads
            seg_ids = result.get("segment_ids")
            if isinstance(seg_ids, str):
                try:
                    seg_ids = json.loads(seg_ids)
                except Exception:
                    seg_ids = []
            result["segment_ids"] = seg_ids if isinstance(seg_ids, list) else []

            md = result.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            result["metadata"] = md if isinstance(md, dict) else None
            return result
        return None

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """批量获取 chunk 元数据"""
        await self.ensure_initialize()

        if not ids:
            return []

        sql = SQL_TEMPLATES_CHUNK_META["get_by_ids"]
        params = [self.workspace, ids]
        results = await self.db.query(sql, params, multirows=True)

        if not results:
            return []

        # Convert timestamps and normalize JSONB fields
        for result in results:
            result["created_at"] = (
                result["created_at"].timestamp() if result.get("created_at") else time.time()
            )
            result["updated_at"] = (
                result["updated_at"].timestamp() if result.get("updated_at") else time.time()
            )
            seg_ids = result.get("segment_ids")
            if isinstance(seg_ids, str):
                try:
                    seg_ids = json.loads(seg_ids)
                except Exception:
                    seg_ids = []
            result["segment_ids"] = seg_ids if isinstance(seg_ids, list) else []

            md = result.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            result["metadata"] = md if isinstance(md, dict) else None

        return results

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        """批量更新或插入 chunk 元数据"""
        await self.ensure_initialize()

        if not data:
            return

        logger.debug(f"[{self.workspace}] Upserting {len(data)} chunk meta records")

        for chunk_id, values in data.items():
            seg_ids = values.get("segment_ids", [])
            if not isinstance(seg_ids, (list, str)):
                seg_ids = []
            if isinstance(seg_ids, list):
                seg_ids = json.dumps(seg_ids)

            md = values.get("metadata")
            if isinstance(md, dict):
                md = json.dumps(md)
            elif md is None:
                md = None
            else:
                md = json.dumps(md) if md else None

            current_time = self._parse_datetime(time.time())
            created_at = (
                self._parse_datetime(values.get("created_at", time.time()))
                if values.get("created_at")
                else current_time
            )

            await self.db.execute(
                SQL_TEMPLATES_CHUNK_META["upsert"],
                {
                    "workspace": self.workspace,
                    "id": chunk_id,
                    "chunk_id": values.get("chunk_id", chunk_id),
                    "segment_ids": seg_ids,
                    "metadata": md,
                    "created_at": created_at,
                    "updated_at": current_time,
                },
            )

    async def delete(self, chunk_ids: list[str]) -> None:
        """删除指定的 chunk 元数据"""
        await self.ensure_initialize()

        if not chunk_ids:
            return

        sql = SQL_TEMPLATES_CHUNK_META["delete"]

        try:
            await self.db.execute(sql, {"workspace": self.workspace, "ids": chunk_ids})
            logger.debug(f"[{self.workspace}] Deleted {len(chunk_ids)} chunk meta records")
        except Exception as e:
            logger.error(f"[{self.workspace}] Error deleting chunk meta: {e}")

    async def filter_keys(self, keys: set[str]) -> set[str]:
        """Return keys that should be processed (not in storage)"""
        await self.ensure_initialize()

        if not keys:
            return set()

        sql = SQL_TEMPLATES_CHUNK_META["filter_keys"]
        params = [self.workspace, list(keys)]

        try:
            results = await self.db.query(sql, params, multirows=True)
            if results:
                existing_keys = [row["id"] for row in results]
            else:
                existing_keys = []

            new_keys = set(k for k in keys if k not in existing_keys)
            return new_keys
        except Exception as e:
            logger.error(f"[{self.workspace}] Error filtering keys: {e}")
            raise

    async def drop(self) -> dict[str, str]:
        """清空当前 workspace 的所有数据"""
        await self.ensure_initialize()

        try:
            sql = SQL_TEMPLATES_CHUNK_META["drop"]
            await self.db.execute(sql, {"workspace": self.workspace})

            logger.info(f"[{self.workspace}] Dropped all chunk meta data")
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            logger.error(f"[{self.workspace}] Error dropping chunk meta: {e}")
            return {"status": "error", "message": str(e)}

    async def is_empty(self) -> bool:
        """检查存储是否为空"""
        await self.ensure_initialize()

        sql = SQL_TEMPLATES_CHUNK_META["is_empty"]
        params = [self.workspace]

        try:
            result = await self.db.query(sql, params)
            return not result.get("has_data", False) if result else True
        except Exception as e:
            logger.error(f"[{self.workspace}] Error checking if storage is empty: {e}")
            return True

    async def index_done_callback(self) -> None:
        """PostgreSQL handles persistence automatically"""
        pass


def create_chunk_meta_storage(storage_type: str = "postgres", **kwargs) -> BaseKVStorage:
    """
    工厂函数：根据配置创建对应的存储实现

    Args:
        storage_type: "postgres" (目前只支持 postgres)
        **kwargs: 传递给存储类的参数

    Returns:
        ChunkMetaStorage 实例
    """
    # BaseKVStorage 构造参数通常要求 embedding_func，但 chunk_meta 并不需要向量化能力。
    # 这里提供一个占位函数以保持接口兼容。
    if "embedding_func" not in kwargs:

        async def _noop_embedding_func(texts: list[str]):  # type: ignore[no-redef]
            return [[0.0] for _ in (texts or [])]

        kwargs["embedding_func"] = _noop_embedding_func

    if storage_type.lower() == "postgres":
        return PGChunkMetaStorage(**kwargs)
    else:
        raise ValueError(f"Unsupported storage_type: {storage_type}. Only 'postgres' is supported.")
