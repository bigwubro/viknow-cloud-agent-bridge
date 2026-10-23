import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, final

from .runtime_primitives import BaseKVStorage, ClientManager, PostgreSQLDB, get_data_init_lock

logger = logging.getLogger(__name__)


class AssetStatus(str, Enum):
    """文档处理状态"""

    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    INDEXING = "indexing"
    POST_PROCESSING = "post_processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETING = "deleting"
    DELETED = "deleted"


def _normalize_asset_status(status: AssetStatus | str) -> AssetStatus:
    try:
        return AssetStatus(status)
    except ValueError as exc:
        raise ValueError(f"Unsupported asset status: {status!r}") from exc


@dataclass
class AssetProcessingStatus:
    """文档处理状态信息"""

    asset_id: str
    status: AssetStatus
    created_at: float
    updated_at: float
    error_message: str | None = None
    progress: float = 0.0  # 0.0 to 1.0
    parse_mode: str | None = None  # "video" | "document"
    file_path: str | None = None
    description: str | None = None  # 进度描述
    metadata: dict[str, Any] | None = None  # {asset_type, page_count/video_duration, chunk_nums}
    file_data: dict[str, Any] | None = None  # 原始 file_data 信息


# ==================== PostgreSQL Implementation ====================

# SQL Templates for Asset Status Storage
SQL_TEMPLATES_ASSET_STATUS = {
    "create_table": """
        CREATE TABLE IF NOT EXISTS HYPERRAG_ASSET_STATUS (
            workspace VARCHAR(255) NOT NULL,
            id VARCHAR(500) NOT NULL,
            asset_id VARCHAR(500) NOT NULL,
            status VARCHAR(50) NOT NULL
                CONSTRAINT hyperrag_asset_status_status_valid CHECK (
                    status IN (
                        'queued', 'parsing', 'chunking', 'indexing',
                        'post_processing', 'completed', 'failed', 'cancelled',
                        'deleting', 'deleted'
                    )
                ),
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            progress FLOAT DEFAULT 0.0,
            error_message TEXT,
            parse_mode VARCHAR(50),
            file_path TEXT,
            metadata JSONB,
            file_data JSONB,
            PRIMARY KEY (workspace, id)
        )
    """,
    "add_metadata_column": """
        ALTER TABLE HYPERRAG_ASSET_STATUS
        ADD COLUMN IF NOT EXISTS metadata JSONB
    """,
    "add_file_data_column": """
        ALTER TABLE HYPERRAG_ASSET_STATUS
        ADD COLUMN IF NOT EXISTS file_data JSONB
    """,
    "repair_legacy_delete_status": """
        UPDATE HYPERRAG_ASSET_STATUS
        SET status = 'deleted'
        WHERE status = 'delete'
    """,
    "add_status_constraint": """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'hyperrag_asset_status_status_valid'
                  AND conrelid = 'hyperrag_asset_status'::regclass
            ) THEN
                ALTER TABLE HYPERRAG_ASSET_STATUS
                ADD CONSTRAINT hyperrag_asset_status_status_valid CHECK (
                    status IN (
                        'queued', 'parsing', 'chunking', 'indexing',
                        'post_processing', 'completed', 'failed', 'cancelled',
                        'deleting', 'deleted'
                    )
                );
            END IF;
        END $$;
    """,
    "create_indexes": [
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_workspace ON HYPERRAG_ASSET_STATUS(workspace)",
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_status ON HYPERRAG_ASSET_STATUS(workspace, status)",
        "CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_updated_at ON HYPERRAG_ASSET_STATUS(workspace, updated_at DESC)",
    ],
    "upsert": """
        INSERT INTO HYPERRAG_ASSET_STATUS
        (workspace, id, asset_id, status, created_at, updated_at, progress,
         error_message, parse_mode, file_path, metadata, file_data)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (workspace, id) DO UPDATE SET
            status = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at,
            progress = EXCLUDED.progress,
            error_message = EXCLUDED.error_message,
            parse_mode = EXCLUDED.parse_mode,
            file_path = EXCLUDED.file_path,
            metadata = EXCLUDED.metadata,
            file_data = EXCLUDED.file_data
    """,
    "update_metadata": """
        UPDATE HYPERRAG_ASSET_STATUS
        SET metadata = (
                COALESCE(metadata, '{}'::jsonb) - COALESCE($5::text[], ARRAY[]::text[])
            ) || $3::jsonb,
            updated_at = $4
        WHERE workspace = $1 AND id = $2
    """,
    "get_by_id": """
        SELECT * FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1 AND id=$2
    """,
    "get_by_ids": """
        SELECT * FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "get_all": """
        SELECT * FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1
    """,
    "filter_keys": """
        SELECT id FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "delete": """
        DELETE FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1 AND id = ANY($2)
    """,
    "drop": """
        DELETE FROM HYPERRAG_ASSET_STATUS 
        WHERE workspace=$1
    """,
    "is_empty": """
        SELECT EXISTS(SELECT 1 FROM HYPERRAG_ASSET_STATUS WHERE workspace=$1 LIMIT 1) as has_data
    """,
}


@final
@dataclass
class AssetStatusStorage(BaseKVStorage):
    """基于 PostgreSQL 的文档状态存储（支持真正的并发访问，全局单例）"""

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

            # Create table and indexes
            await self.db.execute(SQL_TEMPLATES_ASSET_STATUS["create_table"], upsert=True)
            # Backward compatible schema migration (existing table may not have metadata/file_data columns)
            try:
                await self.db.execute(
                    SQL_TEMPLATES_ASSET_STATUS["add_metadata_column"],
                    upsert=True,
                    ignore_if_exists=True,
                )
                await self.db.execute(
                    SQL_TEMPLATES_ASSET_STATUS["add_file_data_column"],
                    upsert=True,
                    ignore_if_exists=True,
                )
            except Exception:
                # Ignore migration errors to avoid blocking startup on older deployments
                pass
            await self.db.execute(SQL_TEMPLATES_ASSET_STATUS["repair_legacy_delete_status"])
            await self.db.execute(SQL_TEMPLATES_ASSET_STATUS["add_status_constraint"])
            for index_sql in SQL_TEMPLATES_ASSET_STATUS["create_indexes"]:
                await self.db.execute(index_sql, upsert=True, ignore_if_exists=True)

        self._initialized = True
        logger.info("AssetStatusStorage initialized (global singleton)")

    async def finalize(self):
        """清理数据库连接"""
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def ensure_initialize(self):
        if not self._initialized:
            await self.initialize()

    def _format_datetime_with_timezone(self, dt):
        """Convert datetime to ISO format string with timezone info"""
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt.isoformat()

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
            logger.warning(f"Unable to parse datetime: {dt_input}")
            return None

    async def get_by_id(self, workspace: str, asset_id: str) -> dict[str, Any] | None:
        """获取文件状态，返回原始存储字典"""
        await self.ensure_initialize()

        sql = SQL_TEMPLATES_ASSET_STATUS["get_by_id"]
        params = [workspace, asset_id]
        result = await self.db.query(sql, params)

        if result:
            # Convert timestamps back to float (Unix timestamp)
            result["created_at"] = (
                result["created_at"].timestamp() if result.get("created_at") else time.time()
            )
            result["updated_at"] = (
                result["updated_at"].timestamp() if result.get("updated_at") else time.time()
            )
            # Normalize metadata: prefer dict; tolerate legacy string payloads
            md = result.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            result["metadata"] = md if isinstance(md, dict) else None
            # Normalize file_data: prefer dict; tolerate legacy string payloads
            fd = result.get("file_data")
            if isinstance(fd, str):
                try:
                    fd = json.loads(fd)
                except Exception:
                    fd = None
            result["file_data"] = fd if isinstance(fd, dict) else None
            return result
        return None

    async def get_by_ids(self, workspace: str, ids: list[str]) -> list[dict[str, Any]]:
        """批量获取文件状态"""
        await self.ensure_initialize()

        if not ids:
            return []

        sql = SQL_TEMPLATES_ASSET_STATUS["get_by_ids"]
        params = [workspace, ids]
        results = await self.db.query(sql, params, multirows=True)

        if not results:
            return []

        # Convert timestamps
        for result in results:
            result["created_at"] = (
                result["created_at"].timestamp() if result.get("created_at") else time.time()
            )
            result["updated_at"] = (
                result["updated_at"].timestamp() if result.get("updated_at") else time.time()
            )
            md = result.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            result["metadata"] = md if isinstance(md, dict) else None
            fd = result.get("file_data")
            if isinstance(fd, str):
                try:
                    fd = json.loads(fd)
                except Exception:
                    fd = None
            result["file_data"] = fd if isinstance(fd, dict) else None

        return results

    async def get_status(self, workspace: str, asset_id: str) -> AssetProcessingStatus | None:
        """获取单个文件的处理状态，返回结构化结果"""
        data_dict = await self.get_by_id(workspace, asset_id)

        if data_dict:
            md = data_dict.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            # Normalize file_data: prefer dict; tolerate legacy payloads
            fd = data_dict.get("file_data")
            if isinstance(fd, str):
                try:
                    fd = json.loads(fd)
                except Exception:
                    fd = None
            return AssetProcessingStatus(
                asset_id=data_dict["asset_id"],
                status=AssetStatus(data_dict["status"]),
                created_at=data_dict["created_at"],
                updated_at=data_dict["updated_at"],
                error_message=data_dict.get("error_message"),
                progress=data_dict.get("progress", 0.0),
                parse_mode=data_dict.get("parse_mode"),
                file_path=data_dict.get("file_path"),
                metadata=md if isinstance(md, dict) else None,
                file_data=fd if isinstance(fd, dict) else None,
            )
        return None

    async def get_status_list(
        self, workspace: str, asset_ids: list[str]
    ) -> dict[str, AssetProcessingStatus]:
        """批量获取文件状态，返回 asset_id -> AssetProcessingStatus"""
        if not asset_ids:
            return {}
        data_list = await self.get_by_ids(workspace, asset_ids)
        out: dict[str, AssetProcessingStatus] = {}
        for data_dict in data_list:
            if not data_dict:
                continue
            md = data_dict.get("metadata")
            if isinstance(md, str):
                try:
                    md = json.loads(md)
                except Exception:
                    md = None
            try:
                asset_id = data_dict["asset_id"]
                fd = data_dict.get("file_data")
                if isinstance(fd, str):
                    try:
                        fd = json.loads(fd)
                    except Exception:
                        fd = None
                out[asset_id] = AssetProcessingStatus(
                    asset_id=asset_id,
                    status=AssetStatus(data_dict["status"]),
                    created_at=data_dict["created_at"],
                    updated_at=data_dict["updated_at"],
                    error_message=data_dict.get("error_message"),
                    progress=data_dict.get("progress", 0.0),
                    parse_mode=data_dict.get("parse_mode"),
                    file_path=data_dict.get("file_path"),
                    metadata=md if isinstance(md, dict) else None,
                    file_data=fd if isinstance(fd, dict) else None,
                )
            except Exception:
                continue
        return out

    async def set_status(
        self,
        workspace: str,
        asset_id: str,
        status: AssetStatus | str,
        progress: float = -1,
        error_message: str | None = None,
        parse_mode: str | None = None,
        file_path: str | None = None,
        metadata: dict[str, Any] | None = None,
        file_data: dict[str, Any] | None = None,
    ):
        """设置文件处理状态，如果不存在则创建"""
        await self.ensure_initialize()
        status = _normalize_asset_status(status)

        # Get existing data or create new
        existing = await self.get_by_id(workspace, asset_id)
        merged_metadata: dict[str, Any] | None = None
        if metadata is not None:
            base = existing.get("metadata") if isinstance(existing, dict) else None
            base = base if isinstance(base, dict) else {}
            merged_metadata = {**base, **metadata}
        elif isinstance(existing, dict) and isinstance(existing.get("metadata"), dict):
            merged_metadata = existing.get("metadata")

        merged_file_data: dict[str, Any] | None = None
        if file_data is not None:
            merged_file_data = file_data
        elif isinstance(existing, dict) and isinstance(existing.get("file_data"), dict):
            merged_file_data = existing.get("file_data")

        if existing:
            # Update existing record
            current_time = self._parse_datetime(time.time())
            data = {
                "workspace": workspace,
                "id": asset_id,
                "asset_id": asset_id,
                "status": status.value,
                "created_at": existing.get("created_at")
                if isinstance(existing.get("created_at"), datetime.datetime)
                else self._parse_datetime(existing.get("created_at")),
                "updated_at": current_time,
                "progress": progress if progress >= 0 else existing.get("progress", 0.0),
                "error_message": error_message
                if error_message is not None
                else existing.get("error_message"),
                "parse_mode": parse_mode if parse_mode else existing.get("parse_mode"),
                "file_path": file_path if file_path else existing.get("file_path"),
                "metadata": json.dumps(merged_metadata) if merged_metadata is not None else None,
                "file_data": json.dumps(merged_file_data) if merged_file_data is not None else None,
            }
        else:
            # Create new record
            current_time = self._parse_datetime(time.time())
            data = {
                "workspace": workspace,
                "id": asset_id,
                "asset_id": asset_id,
                "status": status.value,
                "created_at": current_time,
                "updated_at": current_time,
                "progress": progress if progress >= 0 else 0.0,
                "error_message": error_message,
                "parse_mode": parse_mode,
                "file_path": file_path,
                "metadata": json.dumps(metadata) if metadata is not None else None,
                "file_data": json.dumps(file_data) if file_data is not None else None,
            }

        sql = SQL_TEMPLATES_ASSET_STATUS["upsert"]
        await self.db.execute(
            sql,
            {
                "workspace": data["workspace"],
                "id": data["id"],
                "asset_id": data["asset_id"],
                "status": data["status"],
                "created_at": data["created_at"],
                "updated_at": data["updated_at"],
                "progress": data["progress"],
                "error_message": data["error_message"],
                "parse_mode": data["parse_mode"],
                "file_path": data["file_path"],
                "metadata": data["metadata"],
                "file_data": data["file_data"],
            },
        )

    async def update_metadata(
        self,
        workspace: str,
        asset_id: str,
        metadata: dict[str, Any],
        remove_keys: list[str] | None = None,
    ) -> None:
        """将 metadata 增量合并到已有资产状态，不改动其他状态字段。"""
        keys_to_remove = [str(key) for key in (remove_keys or []) if str(key)]
        if not metadata and not keys_to_remove:
            return

        await self.ensure_initialize()
        await self.db.execute(
            SQL_TEMPLATES_ASSET_STATUS["update_metadata"],
            {
                "$1": workspace,
                "$2": asset_id,
                "$3": json.dumps(metadata, ensure_ascii=False),
                "$4": self._parse_datetime(time.time()),
                "$5": keys_to_remove,
            },
        )

    async def get_all_status(self, workspace: str) -> dict[str, AssetProcessingStatus]:
        """获取所有文档状态"""
        await self.ensure_initialize()

        sql = SQL_TEMPLATES_ASSET_STATUS["get_all"]
        params = [workspace]
        results = await self.db.query(sql, params, multirows=True)

        status_dict = {}
        if results:
            for data in results:
                # Convert timestamps
                created_at = (
                    data["created_at"].timestamp() if data.get("created_at") else time.time()
                )
                updated_at = (
                    data["updated_at"].timestamp() if data.get("updated_at") else time.time()
                )
                md = data.get("metadata")
                if isinstance(md, str):
                    try:
                        md = json.loads(md)
                    except Exception:
                        md = None
                fd = data.get("file_data")
                if isinstance(fd, str):
                    try:
                        fd = json.loads(fd)
                    except Exception:
                        fd = None

                status_dict[data["asset_id"]] = AssetProcessingStatus(
                    asset_id=data["asset_id"],
                    status=AssetStatus(data["status"]),
                    created_at=created_at,
                    updated_at=updated_at,
                    error_message=data.get("error_message"),
                    progress=data.get("progress", 0.0),
                    parse_mode=data.get("parse_mode"),
                    file_path=data.get("file_path"),
                    metadata=md if isinstance(md, dict) else None,
                    file_data=fd if isinstance(fd, dict) else None,
                )

        return status_dict

    async def drop(self, workspace: str) -> dict[str, str]:
        """清空指定 workspace 的所有数据"""
        await self.ensure_initialize()

        try:
            sql = SQL_TEMPLATES_ASSET_STATUS["drop"]
            await self.db.execute(sql, {"workspace": workspace})

            logger.info(f"[{workspace}] Dropped all asset status data")
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            logger.error(f"[{workspace}] Error dropping asset status: {e}")
            return {"status": "error", "message": str(e)}

    async def filter_keys(self, workspace: str, keys: set[str]) -> set[str]:
        """Return keys that should be processed (not in storage)"""
        await self.ensure_initialize()

        if not keys:
            return set()

        sql = SQL_TEMPLATES_ASSET_STATUS["filter_keys"]
        params = [workspace, list(keys)]

        try:
            results = await self.db.query(sql, params, multirows=True)
            if results:
                existing_keys = [row["id"] for row in results]
            else:
                existing_keys = []

            new_keys = set(k for k in keys if k not in existing_keys)
            return new_keys
        except Exception as e:
            logger.error(f"[{workspace}] Error filtering keys: {e}")
            raise

    async def upsert(self, workspace: str, data: dict[str, dict[str, Any]]) -> None:
        """批量更新或插入文档状态"""
        await self.ensure_initialize()

        if not data:
            return

        logger.debug(f"[{workspace}] Upserting {len(data)} asset status records")

        for asset_id, values in data.items():
            status = _normalize_asset_status(values["status"])
            md = values.get("metadata")
            if isinstance(md, dict):
                md = json.dumps(md)
            fd = values.get("file_data")
            if isinstance(fd, dict):
                fd = json.dumps(fd)

            await self.db.execute(
                SQL_TEMPLATES_ASSET_STATUS["upsert"],
                {
                    "workspace": workspace,
                    "id": asset_id,
                    "asset_id": values.get("asset_id", asset_id),
                    "status": status.value,
                    "created_at": self._parse_datetime(values.get("created_at", time.time())),
                    "updated_at": self._parse_datetime(values.get("updated_at", time.time())),
                    "progress": values.get("progress", 0.0),
                    "error_message": values.get("error_message"),
                    "parse_mode": values.get("parse_mode"),
                    "file_path": values.get("file_path"),
                    "metadata": md,
                    "file_data": fd,
                },
            )

    async def delete(self, workspace: str, doc_ids: list[str]) -> None:
        """删除指定的文档状态"""
        await self.ensure_initialize()

        if not doc_ids:
            return

        sql = SQL_TEMPLATES_ASSET_STATUS["delete"]

        try:
            await self.db.execute(sql, {"workspace": workspace, "ids": doc_ids})
            logger.debug(f"[{workspace}] Deleted {len(doc_ids)} asset status records")
        except Exception as e:
            logger.error(f"[{workspace}] Error deleting asset status: {e}")

    async def is_empty(self, workspace: str) -> bool:
        """检查存储是否为空"""
        await self.ensure_initialize()

        sql = SQL_TEMPLATES_ASSET_STATUS["is_empty"]
        params = [workspace]

        try:
            result = await self.db.query(sql, params)
            return not result.get("has_data", False) if result else True
        except Exception as e:
            logger.error(f"[{workspace}] Error checking if storage is empty: {e}")
            return True

    async def index_done_callback(self) -> None:
        """PostgreSQL handles persistence automatically"""
        pass


# ==================== Global Singleton ====================

# 全局单例实例
_asset_status_storage: AssetStatusStorage | None = None


def get_asset_status_storage() -> AssetStatusStorage:
    """
    获取全局唯一的 AssetStatusStorage 单例

    Returns:
        全局单例 AssetStatusStorage 实例
    """
    global _asset_status_storage
    if _asset_status_storage is None:
        if PostgreSQLDB is None:
            raise RuntimeError(
                "PostgreSQL backend is not available. "
                "Please ensure the HyperRAG PostgreSQL dependencies are properly installed."
            )
        _asset_status_storage = AssetStatusStorage(
            namespace="asset_status",
            workspace="",  # 单例不绑定特定 workspace
            global_config={},
            embedding_func=None,
        )
    return _asset_status_storage
