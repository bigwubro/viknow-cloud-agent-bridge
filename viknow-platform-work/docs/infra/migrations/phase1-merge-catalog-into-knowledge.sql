-- Phase 1: catalog in viknow_knowledge (no app_key tenancy).
-- Imports from viknow_hyperrag when catalog tables are empty.
-- Stop viknow-platform-playground / indexing workers before running on shared dev.
--
--   psql "$VIKNOW_HYPERRAG_DSN" -v ON_ERROR_STOP=1 -f docs/infra/migrations/phase1-merge-catalog-into-knowledge.sql

\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS knowledge_libraries (
  library_id text PRIMARY KEY,
  title text NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  deleted boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS knowledge_assets (
  library_id text NOT NULL REFERENCES knowledge_libraries (library_id),
  asset_id text NOT NULL,
  title text NOT NULL,
  modality text NOT NULL,
  asset_path text NOT NULL,
  current_index_version_id text,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (library_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_libraries_active
  ON knowledge_libraries (library_id)
  WHERE NOT deleted;

-- Upgrade legacy catalog tables that still have app_key.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'knowledge_libraries' AND column_name = 'app_key'
  ) THEN
    CREATE TABLE knowledge_libraries_new (
      library_id text PRIMARY KEY,
      title text NOT NULL,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      deleted boolean NOT NULL DEFAULT false
    );
    INSERT INTO knowledge_libraries_new (library_id, title, created_at, updated_at, deleted)
    SELECT DISTINCT ON (library_id)
      library_id, title, created_at, updated_at, deleted
    FROM knowledge_libraries
    ORDER BY library_id, updated_at DESC;

    CREATE TABLE knowledge_assets_new (
      library_id text NOT NULL REFERENCES knowledge_libraries_new (library_id),
      asset_id text NOT NULL,
      title text NOT NULL,
      modality text NOT NULL,
      asset_path text NOT NULL,
      current_index_version_id text,
      created_at timestamptz NOT NULL,
      PRIMARY KEY (library_id, asset_id)
    );
    INSERT INTO knowledge_assets_new (
      library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
    )
    SELECT DISTINCT ON (library_id, asset_id)
      library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
    FROM knowledge_assets
    ORDER BY library_id, asset_id, created_at DESC;

    DROP TABLE knowledge_assets;
    DROP TABLE knowledge_libraries;
    ALTER TABLE knowledge_libraries_new RENAME TO knowledge_libraries;
    ALTER TABLE knowledge_assets_new RENAME TO knowledge_assets;

    CREATE INDEX IF NOT EXISTS idx_knowledge_libraries_active
      ON knowledge_libraries (library_id)
      WHERE NOT deleted;
  END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS dblink;

DO $$
DECLARE
  lib_count bigint;
BEGIN
  SELECT count(*) INTO lib_count FROM public.knowledge_libraries;
  IF lib_count > 0 THEN
    RAISE NOTICE 'catalog already populated — skip dblink import';
    RETURN;
  END IF;

  IF current_database() = 'viknow_hyperrag' THEN
    RAISE EXCEPTION 'connect to viknow_knowledge, not viknow_hyperrag';
  END IF;

  INSERT INTO public.knowledge_libraries (library_id, title, created_at, updated_at, deleted)
  SELECT library_id, title, created_at, updated_at, deleted
  FROM (
    SELECT DISTINCT ON (library_id)
      library_id, title, created_at, updated_at, deleted
    FROM dblink(
      'host=127.0.0.1 port=5432 dbname=viknow_hyperrag user=viknow_ops',
      'SELECT library_id, app_key, title, created_at, updated_at, deleted FROM knowledge_libraries'
    ) AS t(
      library_id text,
      app_key text,
      title text,
      created_at timestamptz,
      updated_at timestamptz,
      deleted boolean
    )
    ORDER BY library_id, updated_at DESC
  ) deduped;

  INSERT INTO public.knowledge_assets (
    library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
  )
  SELECT library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
  FROM (
    SELECT DISTINCT ON (library_id, asset_id)
      library_id, asset_id, title, modality, asset_path, current_index_version_id, created_at
    FROM dblink(
      'host=127.0.0.1 port=5432 dbname=viknow_hyperrag user=viknow_ops',
      'SELECT library_id, app_key, asset_id, title, modality, asset_path, current_index_version_id, created_at FROM knowledge_assets'
    ) AS t(
      library_id text,
      app_key text,
      asset_id text,
      title text,
      modality text,
      asset_path text,
      current_index_version_id text,
      created_at timestamptz
    )
    ORDER BY library_id, asset_id, created_at DESC
  ) deduped;

  RAISE NOTICE 'imported % libraries, % assets',
    (SELECT count(*) FROM public.knowledge_libraries),
    (SELECT count(*) FROM public.knowledge_assets);
END $$;

DO $$
BEGIN
  IF to_regclass('public.hyperrag_asset_status') IS NULL THEN
    RETURN;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'hyperrag_asset_status_asset_fk'
  ) THEN
    RETURN;
  END IF;
  ALTER TABLE hyperrag_asset_status
    ADD CONSTRAINT hyperrag_asset_status_asset_fk
    FOREIGN KEY (workspace, asset_id)
    REFERENCES knowledge_assets (library_id, asset_id)
    NOT VALID;
  RAISE NOTICE 'added NOT VALID FK hyperrag_asset_status -> knowledge_assets';
END $$;
