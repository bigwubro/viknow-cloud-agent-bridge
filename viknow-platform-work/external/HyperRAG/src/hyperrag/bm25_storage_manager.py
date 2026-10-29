"""
BM25 存储管理器 - 管理多个 workspace 的 BM25 存储实例

设计模式与 KG 服务一致：
- 每个 workspace 对应一个 BM25 存储实例
- 提供 get_storage() / create_storage() / delete_storage() API
- 支持 JSON 和 PostgreSQL 两种后端
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class BM25StorageManager:
    """
    BM25 存储管理器

    管理多个 workspace 的 BM25 存储实例，每个 workspace 对应一个独立实例
    与 KG 服务的设计模式保持一致
    """

    def __init__(self, config: Any):
        """
        初始化 BM25 存储管理器

        Args:
            config: 配置对象
        """
        self.config = config
        self._instances: Dict[str, Any] = {}  # workspace -> bm25_storage
        self._locks: Dict[str, asyncio.Lock] = {}  # per-workspace locks

    async def get_storage(self, workspace: str, create_if_missing: bool = True) -> Any:
        """
        获取已存在的 BM25 存储实例，如果不存在则创建

        Args:
            workspace: 工作空间名称
            create_if_missing: 如果不存在是否创建（默认 True）

        Returns:
            BM25 存储实例
        """
        # 使用 per-workspace lock 避免并发创建同一 workspace 的存储
        workspace_lock = self._get_workspace_lock(workspace)
        async with workspace_lock:
            if workspace in self._instances:
                return self._instances[workspace]

            # 创建新实例
            bm25_storage = await self._create_storage(workspace)

            if hasattr(bm25_storage, "initialize"):
                await bm25_storage.initialize()

            self._instances[workspace] = bm25_storage

            logger.info(f"BM25 storage created for workspace: {workspace}")
            return bm25_storage

    def _get_workspace_lock(self, workspace: str) -> asyncio.Lock:
        """获取或创建 workspace 级别的锁"""
        if workspace not in self._locks:
            self._locks[workspace] = asyncio.Lock()
        return self._locks[workspace]

    async def query_cross_workspace(
        self, query: str, *, workspaces: Collection[str], top_k: int
    ) -> list[dict[str, Any]]:
        if getattr(self.config, "bm25_storage_backend", "postgres") != "postgres":
            raise NotImplementedError("Cross-workspace BM25 requires PostgreSQL")
        if not workspaces:
            return []
        # The instance supplies a shared connection, not a per-library query scope.
        storage = await self.get_storage(next(iter(workspaces)))
        return await storage.query_bm25_cross_workspace(query, workspaces=workspaces, top_k=top_k)

    async def _create_storage(self, workspace: str) -> Any:
        """
        创建 BM25 存储实例

        Args:
            workspace: 工作空间名称

        Returns:
            BM25 存储实例
        """
        from .bm25_vdb import create_bm25_storage

        storage_type = getattr(self.config, "bm25_storage_backend", "postgres")
        namespace = getattr(self.config, "bm25_namespace", "hyperrag")

        return create_bm25_storage(
            storage_type=storage_type,
            global_config=self.config.__dict__,
            namespace=namespace,
            workspace=workspace,  # 每个 workspace 独立
        )

    async def delete_storage(self, workspace: str):
        """
        删除指定 workspace 的 BM25 存储实例

        Args:
            workspace: 工作空间名称
        """
        workspace_lock = self._get_workspace_lock(workspace)
        async with workspace_lock:
            if workspace not in self._instances:
                logger.warning(f"BM25 storage not found for workspace: {workspace}")
                return

            bm25_storage = self._instances[workspace]

            try:
                # 调用存储的 drop() 方法删除数据
                await bm25_storage.drop(workspace)
                logger.info(f"BM25 storage data dropped for workspace: {workspace}")
            except Exception as e:
                logger.error(f"Failed to drop BM25 storage for {workspace}: {e}")

            # 关闭存储连接（如果有）
            try:
                if hasattr(bm25_storage, "finalize"):
                    await bm25_storage.finalize()
                elif hasattr(bm25_storage, "close"):
                    await bm25_storage.close()
                elif hasattr(bm25_storage, "_db") and bm25_storage._db:
                    await bm25_storage._db.close()
            except Exception as e:
                logger.error(f"Error closing BM25 storage for {workspace}: {e}")

            # 从管理器中移除实例
            del self._instances[workspace]
            del self._locks[workspace]
            logger.info(f"BM25 storage deleted for workspace: {workspace}")

    async def list_workspaces(self) -> List[str]:
        """
        列出所有有 BM25 存储的 workspace

        Returns:
            workspace 列表
        """
        async with asyncio.Lock():
            return list(self._instances.keys())

    async def close_all(self):
        """关闭所有 BM25 存储实例"""
        # 关闭每个实例
        for workspace, bm25_storage in list(self._instances.items()):
            try:
                if hasattr(bm25_storage, "finalize"):
                    await bm25_storage.finalize()
                elif hasattr(bm25_storage, "close"):
                    await bm25_storage.close()
                elif hasattr(bm25_storage, "_db") and bm25_storage._db:
                    await bm25_storage._db.close()
                logger.info(f"BM25 storage closed for workspace: {workspace}")
            except Exception as e:
                logger.error(f"Error closing BM25 storage for {workspace}: {e}")

        # 清空实例和锁
        self._instances.clear()
        self._locks.clear()
        logger.info("All BM25 storage instances closed")
