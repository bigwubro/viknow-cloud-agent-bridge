# Knowledge index job persistence

## Policy

All Playground knowledge **index job** and **index status** state is authoritative in **Postgres** (`viknow3_knowledge` via `VIKNOW_HYPERRAG_DSN`). The browser must not use `localStorage` as the source of truth for `job_id`, running/failed status, or indexed version.

| Concern | Source of truth |
|--------|------------------|
| Asset catalog + `current_index_version_id` | `knowledge_assets` |
| Index job rows (`job_id`, status, errors, timestamps) | `knowledge_index_jobs` |
| Engine pipeline status (when present) | `hyperrag_asset_status` (synced into catalog on terminal states) |
| Ephemeral in-flight progress | Redis (read-only fallback in `GET …/index-status`; rows are upserted when seen) |

There is **no `app_key` column** on catalog or job tables; libraries and assets are keyed by `library_id` / `asset_id` only.

## Schema: `knowledge_index_jobs`

```sql
job_id          text PRIMARY KEY
library_id      text NOT NULL → knowledge_libraries
asset_id        text NOT NULL → knowledge_assets (library_id, asset_id)
status          text NOT NULL   -- API job status (pending, running, done, failed, …)
error_message   text
created_at      timestamptz NOT NULL
completed_at    timestamptz     -- set on terminal status
```

Latest job per asset: `ORDER BY created_at DESC LIMIT 1` on `(library_id, asset_id)`.

Migration: `docs/infra/migrations/knowledge-index-jobs.sql` (also applied at process startup via `store._ensure()`).

## HTTP endpoints

| Method | Path | Role |
|--------|------|------|
| `POST` | `/api/v1/knowledge/index-jobs` | Creates HyperRAG job **and** inserts `knowledge_index_jobs` row |
| `GET` | `/api/v1/knowledge/index-jobs/{job_id}` | Polls HyperRAG; **updates** job row + catalog on terminal success |
| `GET` | `/api/v1/knowledge/libraries/{library_id}/assets/{asset_id}/index-status` | UI refresh / “simulate reload”; reads catalog → job table → `hyperrag_asset_status` → Redis |
| `GET` | `/api/v1/knowledge/libraries/{library_id}/assets/index-status-batch` | Playground library table: all asset statuses in one request |
| `GET` | `/api/v1/knowledge/libraries/{library_id}/assets/{asset_id}/index-jobs/latest` | Optional explicit latest job row |

## Write paths

1. **Create** — `access.rag.service.create_index_job` → `knowledge_store.insert_index_job` after `HYPERAG.create_index`.
2. **Poll** — `get_index_job` → `sync_index_job_from_api` (upsert job + `current_index_version_id` on success).
3. **Status read** — `get_asset_index_status` may backfill catalog from `hyperrag_asset_status` and upsert matching job rows on terminal engine states.

## Playground UI

`ui/static/playground/assets/library.js` keeps an in-memory `Map` only for the current page session (polling/modal). On load it calls `index-status-batch` once; **no** `viknow_playground_index_jobs:*` keys.

## Tests

- `tests/test_index_status_api_integration.py` — job row after create, `index-status` after simulated refresh (API-only).
- `tests/test_knowledge_index_status.py` — unit tests for completion writeback and HyperRAG backfill.
