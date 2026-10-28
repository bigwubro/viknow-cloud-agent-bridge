# Knowledge index job persistence

## Policy

Playground **index job** and **index status** state live in Postgres (`viknow3_knowledge` via `VIKNOW_HYPERRAG_DSN`). Asset **catalog** (titles, blobs) is **viknow-storage only** — ViKnow does not mirror `knowledge_libraries` / `knowledge_assets`.

| Concern | Source of truth |
|--------|------------------|
| Libraries + asset metadata + bytes | **viknow-storage** |
| Index job rows (`job_id`, status, errors, timestamps) | `catalog.knowledge_index_jobs` |
| Engine pipeline status (when present) | `hyperrag.hyperrag_asset_status` (+ job row upserts) |
| Ephemeral in-flight progress | Redis (read-only fallback in `GET …/index-status`) |

`current_index_version_id` in API responses is **derived** from the latest successful job id or HyperRAG status row — not stored on a catalog asset row.

## Schema: `knowledge_index_jobs`

```sql
job_id          text PRIMARY KEY
library_id      text NOT NULL
asset_id        text NOT NULL
status          text NOT NULL
error_message   text
created_at      timestamptz NOT NULL
completed_at    timestamptz
```

Latest job per asset: `ORDER BY created_at DESC LIMIT 1` on `(library_id, asset_id)`.

Startup migration (`store._ensure()`): drops legacy catalog tables if present and ensures `knowledge_index_jobs` without foreign keys to catalog.

## HTTP endpoints

| Method | Path | Role |
|--------|------|------|
| `POST` | `/api/v1/knowledge/index-jobs` | HyperRAG job + `knowledge_index_jobs` insert |
| `GET` | `/api/v1/knowledge/index-jobs/{job_id}` | Poll HyperRAG; upsert job row |
| `GET` | `…/index-status` | storage asset check → jobs → HyperRAG → Redis |
| `GET` | `…/index-status-batch` | Lists asset ids from storage, then status per id |
| `GET` | `…/index-jobs/latest` | Latest persisted job row |

## Playground UI

`library.js`: libraries/assets from storage; `index-status-batch` from ViKnow API.

## Tests

- `tests/test_index_status_api_integration.py`
- `tests/test_knowledge_index_status.py`
- `tests/test_storage_index_job.py`
