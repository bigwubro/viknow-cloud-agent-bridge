-- Dev-only cleanup for ViKnow 3 knowledge / access test data.
-- Default target: viknow3_knowledge_test (set search_path or connect to that DB).
-- Does NOT run against production DB names (guarded below).
--
-- Removes:
--   - knowledge libraries whose library_id starts with 'e2e-' or 'lib-idx-'
--   - related knowledge_assets and knowledge_index_jobs (CASCADE via FK on assets)
--   - optional: playground test users matching 'e2e-%' or 'usr_test' in viknow3_access_test
--
-- Usage (236 dev):
--   psql "$VIKNOW_HYPERRAG_DSN" -v ON_ERROR_STOP=1 -f scripts/cleanup-dev-test-data.sql
--   psql "$VIKNOW_ACCESS_DSN" -v ON_ERROR_STOP=1 -f scripts/cleanup-dev-test-data.sql
--     (only access section runs when current_database() ends with _test)

BEGIN;

DO $$
DECLARE
  db_name text := current_database();
BEGIN
  IF db_name NOT LIKE '%\_test' ESCAPE '\' AND db_name <> 'viknow3_knowledge_test' THEN
    RAISE EXCEPTION 'refusing cleanup on non-test database: %', db_name;
  END IF;
END $$;

DO $$
BEGIN
  IF to_regclass('catalog.knowledge_libraries') IS NOT NULL THEN
    DELETE FROM catalog.knowledge_index_jobs
    WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';
    DELETE FROM catalog.knowledge_assets
    WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';
    DELETE FROM catalog.knowledge_libraries
    WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';
  END IF;
  IF to_regclass('access.users') IS NOT NULL THEN
    DELETE FROM access.users
    WHERE id LIKE 'e2e-%' OR id = 'usr_test';
  END IF;
END $$;

COMMIT;
