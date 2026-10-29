# Catalog + engine DB (implemented)

**Status (2026-10-22):** Phase 1 on `main` — one Postgres database **`viknow3_knowledge`** for ViKnow catalog tables and HyperRAG engine tables. **`app_key` is not stored** in `knowledge_libraries` / `knowledge_assets`; HTTP may still require `Authorization` / app registration via `require_app`, but rows are keyed by `library_id` only.

## Schema

| Table | Keys |
| --- | --- |
| `knowledge_libraries` | `library_id` PK |
| `knowledge_assets` | `(library_id, asset_id)` PK, FK → `knowledge_libraries` |
| `hyperrag_asset_status` | `(workspace, id)` PK; optional `NOT VALID` FK `(workspace, asset_id)` → `knowledge_assets(library_id, asset_id)` |

`workspace` in HyperRAG equals catalog `library_id`.

## Migration

Run on dev (236):

```bash
docker exec -i <postgres-container> psql -U viknow_ops -d viknow3_knowledge -v ON_ERROR_STOP=1 \
  < docs/infra/migrations/phase1-merge-catalog-into-knowledge.sql
```

## 236 env (`pg-demo.env`)

Set **`VIKNOW_HYPERRAG_DSN`** to the same database as ops `postgres_connection.database` (e.g. `…/viknow3_knowledge`). Retire separate `viknow_hyperrag` DSN for the API after migration.

Tests: point **`VIKNOW_HYPERRAG_DSN`** at **`viknow3_knowledge_test`** on externally provisioned Postgres (see `docs/external-infra.md`).

Application code runs `_ensure()` + legacy `app_key` column migration on startup (`access/knowledge/store.py`).

---

## Summary

| Question | Answer |
| --- | --- |
| **Feasible?** | **Yes**, with phased work. Phase 1 (same DB, `public` schema, no HyperRAG query fork) is **low risk**. Full alignment (FKs, `app_key`, schema split) needs **moderate** HyperRAG + viknow3 changes. |
| **Target database** | **`viknow3_knowledge`** (already used by ops `postgres_connection` for the engine). Retire **`viknow_hyperrag`** as a separate catalog DB after migration. |
| **HyperRAG submodule change for Phase 1?** | **No** — only DSN / data move. |
| **HyperRAG submodule change for FK + `app_key`?** | **Yes** — `hyperrag_asset_status` (and ideally other `workspace`-keyed tables) need `app_key` + SQL template updates. |

## Current state (236 snapshot)

Two logical “knowledge” Postgres databases on `127.0.0.1:16432`:

| Database | Connection | Tables (representative) |
| --- | --- | --- |
| **`viknow_hyperrag`** | `VIKNOW_HYPERRAG_DSN` in `pg-demo.env` | `knowledge_libraries`, `knowledge_assets` |
| **`viknow3_knowledge`** | ops `postgres_connection.database` → materialized as `POSTGRES_DATABASE` for HyperRAG | `hyperrag_asset_status`, `hyperrag_bm25_chunks`, `hyperrag_knowledgebase_info`, discovery tables; schema **`hyperrag_kg`**: `kg_*_vectors` |

ViKnow 3 code paths:

- **Catalog**: `src/viknow3/access/knowledge/store.py` → `connect(HYPERRAG)` / `VIKNOW_HYPERRAG_DSN`, `_ensure()` creates `knowledge_*` with composite PK `(library_id, app_key[, asset_id])`.
- **Engine**: `src/viknow3/application/hyperrag/ops_bootstrap.py` copies ops **`postgres_connection`** into HyperRAG env (`POSTGRES_HOST` … `POSTGRES_DATABASE`).
- **Workspace mapping**: `HyperRAGAdapter` passes `workspace=library_id` (no `app_key`).

HyperRAG storage (submodule `external/HyperRAG`):

- `asset_status.py` — `CREATE TABLE IF NOT EXISTS hyperrag_asset_status` in **`public`**, PK `(workspace, id)`; columns include `asset_id` but **not** `app_key`.
- `bm25_vdb.py`, `chunk_meta.py`, `knowledgebase_info.py`, `knowledge_discovery.py` — same pattern: unqualified `hyperrag_*` in **`public`**, keyed by `workspace`.
- `kg_vector_store.py` — **configurable schema** (`hyperrag_kg` on 236), qualified `schema.table` in SQL.

