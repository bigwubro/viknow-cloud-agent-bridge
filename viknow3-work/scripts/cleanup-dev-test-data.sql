-- Dev-only cleanup for ViKnow 3 knowledge / access test data.
-- Default target: viknow3_knowledge_test (set search_path or connect to that DB).
-- Does NOT run against production DB names (guarded below).
--
-- Removes:
--   - knowledge libraries whose library_id starts with 'e2e-' or 'lib-idx-'
--   - related knowledge_assets and knowledge_index_jobs (CASCADE via FK on assets)
--   - optional: playground test users matching 'e2e-%' or 'usr_test' in viknow_access_test
--
-- Usage (236 dev):
--   psql "$VIKNOW_HYPERRAG_DSN" -v ON_ERROR_STOP=1 -f scripts/cleanup-dev-test-data.sql
--   psql "$VIKNOW_ACCESS_DSN" -v ON_ERROR_STOP=1 -c "SET search_path TO public;" -f scripts/cleanup-dev-test-data.sql
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

-- Knowledge DB (viknow3_knowledge_test / viknow_hyperrag_test)
DELETE FROM knowledge_index_jobs
WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';

DELETE FROM knowledge_assets
WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';

DELETE FROM knowledge_libraries
WHERE library_id LIKE 'e2e-%' OR library_id LIKE 'lib-idx-%';

-- Access DB: run when connected to viknow_access_test (users table exists)
DO $$
BEGIN
  IF to_regclass('public.users') IS NOT NULL THEN
    DELETE FROM users
    WHERE id LIKE 'e2e-%' OR id = 'usr_test';
  END IF;
END $$;

COMMIT;
