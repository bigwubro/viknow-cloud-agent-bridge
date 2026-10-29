#!/usr/bin/env python3
"""
HyperRAG 数据迁移工具：JSON -> PostgreSQL

用途：将现有的 JSON 文件数据迁移到 PostgreSQL 数据库

使用方法：
    python migrate_json_to_postgres.py --workspace default --working-dir ./hyperrag-storage

注意事项：
1. 确保目标 PostgreSQL 数据库已运行且配置正确
2. 确保已执行 migrate_to_postgres.sql 创建表结构
3. 迁移过程不会删除原有 JSON 文件（安全保留）
4. 可以重复执行（幂等操作）
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hyperrag.asset_status import PGAssetStatusStorage
from loguru import logger


async def migrate_asset_status(json_file: Path, workspace: str, working_dir: str) -> int:
    """
    迁移 Asset Status 数据

    Returns:
        迁移的记录数
    """
    if not json_file.exists():
        logger.warning(f"Asset status JSON file not found: {json_file}")
        return 0

    logger.info(f"Migrating asset status from {json_file}")

    # 读取 JSON 数据
    with open(json_file, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    if not json_data:
        logger.info("No asset status data to migrate")
        return 0

    # 创建 PostgreSQL 存储实例
    pg_storage = PGAssetStatusStorage(
        namespace="asset_status",
        workspace=workspace,
        global_config={"working_dir": working_dir},
        embedding_func=None,
    )

    await pg_storage.initialize()

    # 迁移数据
    migrated = 0
    for asset_id, data in json_data.items():
        try:
            await pg_storage.upsert({asset_id: data})
            migrated += 1
            if migrated % 100 == 0:
                logger.info(f"Migrated {migrated} asset status records...")
        except Exception as e:
            logger.error(f"Failed to migrate asset {asset_id}: {e}")

    await pg_storage.finalize()

    logger.info(f"Successfully migrated {migrated} asset status records")
    return migrated


async def main():
    parser = argparse.ArgumentParser(description="Migrate HyperRAG data from JSON to PostgreSQL")
    parser.add_argument(
        "--workspace", type=str, default="default", help="Workspace name (default: default)"
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        default="./hyperrag-storage",
        help="HyperRAG working directory (default: ./hyperrag-storage)",
    )
    parser.add_argument(
        "--skip-asset-status", action="store_true", help="Skip asset status migration"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run: show what would be migrated without actually migrating",
    )

    args = parser.parse_args()

    working_dir = Path(args.working_dir)
    workspace = args.workspace

    workspace_dir = working_dir / workspace
    if not workspace_dir.exists():
        logger.error(f"Workspace directory not found: {workspace_dir}")
        sys.exit(1)

    logger.info(f"Starting migration for workspace: {workspace}")
    logger.info(f"Working directory: {working_dir}")

    total_migrated = 0

    # 迁移 Asset Status
    if not args.skip_asset_status:
        asset_status_file = workspace_dir / "kv_store_asset_status.json"
        if args.dry_run:
            if asset_status_file.exists():
                with open(asset_status_file, "r") as f:
                    data = json.load(f)
                logger.info(f"[DRY RUN] Would migrate {len(data)} asset status records")
        else:
            count = await migrate_asset_status(asset_status_file, workspace, str(working_dir))
            total_migrated += count

    if args.dry_run:
        logger.info("[DRY RUN] Migration simulation completed")
    else:
        logger.info(f"Migration completed! Total records migrated: {total_migrated}")
        logger.info(
            "\n" + "=" * 60 + "\n"
            "Important: Original JSON files are preserved.\n"
            "To switch to PostgreSQL backend, update your HyperRAGConfig:\n"
            "  config.asset_status_storage_backend = 'postgres'\n" + "=" * 60
        )


if __name__ == "__main__":
    asyncio.run(main())
