"""
BM25 存储模块

提供 BM25 全文搜索的存储后端，支持 PostgreSQL（生产环境）和 JSON（开发环境）两种实现。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime_primitives import ClientManager, PostgreSQLDB

logger = logging.getLogger(__name__)

# ==================== SQL Templates ====================

SQL_TEMPLATES_BM25 = {
    # 创建 BM25 表
    "create_table": """
        CREATE TABLE IF NOT EXISTS HYPERRAG_BM25_CHUNKS (
            workspace VARCHAR(255) NOT NULL,
            id VARCHAR(255) NOT NULL,
            full_doc_id VARCHAR(256),
            chunk_order_index INTEGER,
            content TEXT NOT NULL,
            content_tokenized TEXT NOT NULL,
            tokens INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (workspace, id)
        );
    """,
    # 创建索引
    "create_index_workspace": """
        CREATE INDEX IF NOT EXISTS idx_bm25_workspace
        ON HYPERRAG_BM25_CHUNKS(workspace);
    """,
    "create_index_full_doc": """
        CREATE INDEX IF NOT EXISTS idx_bm25_full_doc
        ON HYPERRAG_BM25_CHUNKS(workspace, full_doc_id);
    """,
    # BM25 全文搜索索引（使用 pg_textsearch 扩展）
    "create_index_bm25": """
        CREATE INDEX IF NOT EXISTS idx_bm25_content
        ON HYPERRAG_BM25_CHUNKS
        USING bm25(content_tokenized)
        WITH (text_config = 'simple');
    """,
    # Upsert（插入或更新）
    "upsert": """
        INSERT INTO HYPERRAG_BM25_CHUNKS
        (workspace, id, full_doc_id, chunk_order_index, content, content_tokenized, tokens)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (workspace, id)
        DO UPDATE SET
            full_doc_id = EXCLUDED.full_doc_id,
            chunk_order_index = EXCLUDED.chunk_order_index,
            content = EXCLUDED.content,
            content_tokenized = EXCLUDED.content_tokenized,
            tokens = EXCLUDED.tokens;
    """,
    # BM25 查询
    "query_bm25": """
        SELECT id,
               content_tokenized <@> to_bm25query($1, 'idx_bm25_content') AS bm25_score
        FROM HYPERRAG_BM25_CHUNKS
        WHERE workspace = $2
        ORDER BY content_tokenized <@> to_bm25query($1, 'idx_bm25_content')
        LIMIT $3;
    """,
    # 删除指定 chunks
    "delete_by_ids": """
        DELETE FROM HYPERRAG_BM25_CHUNKS
        WHERE workspace = $1 AND id = ANY($2);
    """,
    # 删除 workspace 的所有数据
    "drop_by_workspace": """
        DELETE FROM HYPERRAG_BM25_CHUNKS
        WHERE workspace = $1;
    """,
    # 删除指定文档的所有 chunks
    "delete_by_doc_id": """
        DELETE FROM HYPERRAG_BM25_CHUNKS
        WHERE workspace = $1 AND full_doc_id = $2;
    """,
    # 查询 workspace 是否有数据
    "is_empty": """
        SELECT NOT EXISTS(SELECT 1 FROM HYPERRAG_BM25_CHUNKS WHERE workspace=$1 LIMIT 1) as is_empty;
    """,
}


# ==================== JSON Implementation ====================


@dataclass
class JsonBM25Storage:
    """基于 JSON 文件的 BM25 存储（开发环境）"""

    global_config: dict[str, Any]
    namespace: str
    workspace: str
    storage_path: Path | None = field(default=None)
    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    _initialized: bool = field(default=False, init=False)

    def __post_init__(self):
        """初始化存储路径"""
        if self.storage_path is None:
            working_dir = self.global_config.get("working_dir", "./hyperrag-storage")
            path = Path(working_dir) / self.namespace / f"{self.workspace}_bm25.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path = path

    async def initialize(self):
        """从 JSON 文件加载数据"""
        if self._initialized:
            return

        if self.storage_path.exists():
            try:
                with open(self.storage_path, encoding="utf-8") as f:
                    self.data = json.load(f)
                logger.info(f"Loaded BM25 data from {self.storage_path}")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Failed to load BM25 data: {e}, starting fresh")

        self._initialized = True

    async def upsert(
        self,
        workspace: str,
        id: str,
        content: str,
        content_tokenized: str,
        full_doc_id: str | None = None,
        chunk_order_index: int | None = None,
        tokens: int | None = None,
    ):
        """插入或更新 chunk"""
        self.data[id] = {
            "workspace": workspace,
            "id": id,
            "full_doc_id": full_doc_id,
            "chunk_order_index": chunk_order_index,
            "content": content,
            "content_tokenized": content_tokenized,
            "tokens": tokens,
            "created_at": time.time() if self.storage_path else None,
        }
        self._persist()

    async def query_bm25(
        self,
        query: str,
        workspace: str,
        top_k: int = 30,
    ) -> list[tuple[str, float]]:
        """
        执行 BM25 检索（简化版：仅返回 ID，不计算分数）

        注意：JSON 实现为简化版，生产环境应使用 PostgreSQL
        """
        # 简化实现：只返回匹配 workspace 的 chunks
        matches = [
            (chunk_id, 0.0)  # 简化版不计算分数
            for chunk_id, chunk_data in self.data.items()
            if chunk_data.get("workspace") == workspace
        ]
        return matches[:top_k]

    async def delete(self, workspace: str, ids: list[str]):
        """删除指定 chunks"""
        for chunk_id in ids:
            if chunk_id in self.data:
                del self.data[chunk_id]
        self._persist()

    async def drop(self, workspace: str):
        """删除 workspace 的所有数据"""
        keys_to_delete = [
            chunk_id
            for chunk_id, chunk_data in self.data.items()
            if chunk_data.get("workspace") == workspace
        ]
        for chunk_id in keys_to_delete:
            del self.data[chunk_id]
        self._persist()

    async def delete_by_doc_id(self, workspace: str, doc_id: str):
        """删除指定文档的所有 chunks"""
        keys_to_delete = [
            chunk_id
            for chunk_id, chunk_data in self.data.items()
            if chunk_data.get("workspace") == workspace and chunk_data.get("full_doc_id") == doc_id
        ]
        for chunk_id in keys_to_delete:
            del self.data[chunk_id]
        self._persist()

    def _persist(self):
        """持久化到 JSON 文件"""
        if self.storage_path:
            try:
                with open(self.storage_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except OSError as e:
                logger.error(f"Failed to persist BM25 data: {e}")


# ==================== PostgreSQL Implementation ====================


@dataclass
class PGBM25Storage:
    """PostgreSQL BM25 存储（生产环境）"""

    global_config: dict[str, Any]
    namespace: str
    workspace: str
    db: PostgreSQLDB | None = field(default=None)
    _initialized: bool = field(default=False, init=False)

    async def initialize(self):
        """初始化数据库连接，创建表和索引"""
        if self._initialized:
            return

        if ClientManager is None:
            raise RuntimeError(
                "PostgreSQL vector backend requires HyperRAG database configuration. "
                "Please ensure PostgreSQL is properly configured."
            )

        # 获取数据库客户端
        self.db = await ClientManager.get_client()

        # 创建表
        await self.db.execute(SQL_TEMPLATES_BM25["create_table"], upsert=True)

        # 创建索引
        for index_name in ["create_index_workspace", "create_index_full_doc", "create_index_bm25"]:
            try:
                await self.db.execute(
                    SQL_TEMPLATES_BM25[index_name], upsert=True, ignore_if_exists=True
                )
            except Exception as e:
                # 索引创建失败不应阻塞启动（pg_textsearch 可能未安装）
                logger.warning(f"Failed to create BM25 index {index_name}: {e}")

        self._initialized = True
        logger.info("PGBM25Storage initialized")

    async def upsert(
        self,
        workspace: str,
        id: str,
        content: str,
        content_tokenized: str,
        full_doc_id: str | None = None,
        chunk_order_index: int | None = None,
        tokens: int | None = None,
    ):
        """插入或更新 BM25 chunk"""
        await self.db.execute(
            SQL_TEMPLATES_BM25["upsert"],
            {
                "$1": workspace,
                "$2": id,
                "$3": full_doc_id,
                "$4": chunk_order_index,
                "$5": content,
                "$6": content_tokenized,
                "$7": tokens,
            },
        )

    async def query_bm25(
        self,
        query: str,
        workspace: str,
        top_k: int = 30,
    ) -> list[tuple[str, float]]:
        """
        执行 BM25 检索

        Returns:
            List[Tuple[chunk_id, bm25_score]]
        """
        results = await self.db.query(
            SQL_TEMPLATES_BM25["query_bm25"], [query, workspace, top_k], multirows=True
        )

        return [(row["id"], row["bm25_score"]) for row in results]

    async def query_bm25_cross_workspace(
        self, query: str, *, workspaces: Collection[str], top_k: int = 30
    ) -> list[dict[str, Any]]:
        if not workspaces:
            return []
        sql = """
            SELECT workspace, id,
                   content_tokenized <@> to_bm25query($1, 'idx_bm25_content') AS bm25_score
            FROM HYPERRAG_BM25_CHUNKS
            WHERE workspace = ANY($2)
            ORDER BY content_tokenized <@> to_bm25query($1, 'idx_bm25_content')
            LIMIT $3
        """
        return await self.db.query(sql, [query, list(workspaces), top_k], multirows=True)

    async def delete(self, workspace: str, ids: list[str]):
        """删除指定 chunks"""
        await self.db.execute(
            SQL_TEMPLATES_BM25["delete_by_ids"],
            {
                "$1": workspace,
                "$2": ids,
            },
        )

    async def delete_by_doc_id(self, workspace: str, doc_id: str):
        """删除指定文档的所有 chunks"""
        await self.db.execute(
            SQL_TEMPLATES_BM25["delete_by_doc_id"],
            {
                "$1": workspace,
                "$2": doc_id,
            },
        )

    async def upsert_batch(self, workspace: str, entries: list[dict[str, Any]]):
        """批量插入或更新 BM25 chunks"""
        for entry in entries:
            chunk_id = entry["chunk_id"]
            content = entry["text"]
            tokens = entry["tokens"]
            chunk_order_index = entry["chunk_order_index"]
            full_doc_id = entry["full_doc_id"]
            # Convert tokens list to space-separated string
            content_tokenized = tokens

            await self.db.execute(
                SQL_TEMPLATES_BM25["upsert"],
                {
                    "$1": workspace,
                    "$2": chunk_id,
                    "$3": full_doc_id,  # full_doc_id (will be set by handler in Task 5)
                    "$4": chunk_order_index,
                    "$5": content,
                    "$6": content_tokenized,
                    "$7": len(tokens.split()),  # tokens count
                },
            )

    async def drop(self, workspace: str):
        """删除 workspace 的所有数据"""
        await self.db.execute(
            SQL_TEMPLATES_BM25["drop_by_workspace"],
            {
                "$1": workspace,
            },
        )

    async def finalize(self):
        """释放数据库连接"""
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None


# ==================== Factory Function ====================


def create_bm25_storage(
    storage_type: str,
    global_config: dict[str, Any],
    namespace: str,
    workspace: str,
) -> Any:
    """
    工厂函数：创建 BM25 存储实例

    Args:
        storage_type: 存储类型，"json" 或 "postgres"
        global_config: 全局配置
        namespace: 命名空间
        workspace: 工作空间 ID

    Returns:
        BM25 存储实例

    Raises:
        ValueError: 不支持的存储类型
    """
    if storage_type == "json":
        return JsonBM25Storage(
            global_config=global_config,
            namespace=namespace,
            workspace=workspace,
        )
    elif storage_type == "postgres":
        return PGBM25Storage(
            global_config=global_config,
            namespace=namespace,
            workspace=workspace,
        )
    else:
        raise ValueError(f"Unsupported storage type: {storage_type}. Must be 'json' or 'postgres'")
