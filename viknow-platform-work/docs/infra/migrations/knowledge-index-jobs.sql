-- Persisted catalog index jobs (no app_key). Safe to re-run.
-- Target: viknow_knowledge (same DB as knowledge_libraries / HyperRAG engine tables).

CREATE TABLE IF NOT EXISTS knowledge_index_jobs (
    job_id text PRIMARY KEY,
    library_id text NOT NULL REFERENCES knowledge_libraries (library_id),
    asset_id text NOT NULL,
    status text NOT NULL,
    error_message text,
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    FOREIGN KEY (library_id, asset_id)
        REFERENCES knowledge_assets (library_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_index_jobs_asset_latest
    ON knowledge_index_jobs (library_id, asset_id, created_at DESC);

-- If an old row reused the same job_id for a different asset, re-point it on upsert (see insert_index_job).
