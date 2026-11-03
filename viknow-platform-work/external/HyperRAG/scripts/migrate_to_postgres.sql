-- ================================================
-- HyperRAG PostgreSQL Migration Script
-- ================================================
-- 用途：创建 HyperRAG 所需的 PostgreSQL 表和索引
-- 使用方法：
--   psql -U postgres -d your_database -f migrate_to_postgres.sql
--
-- 注意事项：
-- 1. 确保 PostgreSQL 版本 >= 12
-- 2. 视频片段存储需要安装 pgvector 扩展
-- 3. 本脚本是幂等的，可以重复执行
-- ================================================

-- 启用 pgvector 扩展（用于视频片段向量存储）
-- 如果不需要视频功能，可以注释掉这一行
CREATE EXTENSION IF NOT EXISTS vector;

-- ================================================
-- 1. Asset Status Storage 表
-- ================================================
-- 用于存储文档/视频处理状态

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
    PRIMARY KEY (workspace, id)
);

-- Correct the legacy value before constraining existing installations.
UPDATE HYPERRAG_ASSET_STATUS
SET status = 'deleted'
WHERE status = 'delete';

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

-- 为 Asset Status 创建索引
CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_workspace 
    ON HYPERRAG_ASSET_STATUS(workspace);

CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_status 
    ON HYPERRAG_ASSET_STATUS(workspace, status);

CREATE INDEX IF NOT EXISTS idx_hyperrag_asset_status_updated_at 
    ON HYPERRAG_ASSET_STATUS(workspace, updated_at DESC);

-- ================================================
-- 验证表创建
-- ================================================
-- 查看已创建的表
SELECT 
    table_name, 
    pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) as size
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name = 'hyperrag_asset_status'
ORDER BY table_name;

-- 查看索引
SELECT 
    tablename, 
    indexname, 
    indexdef 
FROM pg_indexes 
WHERE tablename = 'hyperrag_asset_status'
ORDER BY tablename, indexname;

-- ================================================
-- 清理脚本（谨慎使用！）
-- ================================================
-- 如果需要删除表和数据，取消下面语句的注释

-- DROP TABLE IF EXISTS HYPERRAG_ASSET_STATUS CASCADE;

-- ================================================
-- 结束
-- ================================================

