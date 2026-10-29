"""Playground index status: catalog, persisted index jobs, HyperRAG asset_status, optional Redis."""

from __future__ import annotations

from typing import Any

from viknow_platform.access.knowledge import store
from viknow_platform.access.knowledge.service import KnowledgeError

_INDEX_DONE = frozenset({"done", "completed"})
_INDEX_FAILED = frozenset({"failed", "cancelled"})
_INDEX_RUNNING = frozenset({"pending", "running", "queued"})

_HYPERRAG_INDEXED = frozenset({"completed"})
_HYPERRAG_FAILED = frozenset({"failed", "cancelled"})
_HYPERRAG_RUNNING = frozenset(
    {"queued", "parsing", "chunking", "indexing", "post_processing", "deleting"}
)


def _redis_job_snapshot(library_id: str, asset_id: str) -> dict[str, Any] | None:
    try:
        from viknow_platform.application.hyperrag.config import load_app_config
        from viknow_platform.application.hyperrag.job_progress import (
            JOB_STATUS_CANCELLED,
            JOB_STATUS_COMPLETED,
            JOB_STATUS_FAILED,
            JOB_STATUS_RUNNING,
            HyperRAGJobProgressReader,
        )
    except Exception:
        return None
    try:
        progress = HyperRAGJobProgressReader(load_app_config()).get_job_by_asset(library_id, asset_id)
    except Exception:
        return None
    if progress is None:
        return None
    raw = str(progress.status or "").strip().lower()
    if raw in {JOB_STATUS_COMPLETED, "done", "completed"}:
        ui_status = "indexed"
    elif raw in {JOB_STATUS_FAILED, JOB_STATUS_CANCELLED, "failed", "cancelled"}:
        ui_status = "failed"
    elif raw in {JOB_STATUS_RUNNING, "running", "pending", "queued"}:
        ui_status = "indexing"
    else:
        ui_status = "indexing"
    return {
        "status": ui_status,
        "job_id": progress.job_id or None,
        "error_message": progress.error_message,
        "engine_status": raw,
    }


def _version_from_hyperrag_row(row: dict[str, Any]) -> str:
    raw_id = str(row.get("id") or "").strip()
    if raw_id:
        return raw_id
    workspace = str(row.get("workspace") or "").strip()
    asset_id = str(row.get("asset_id") or "").strip()
    return f"hyperrag:{workspace}:{asset_id}"


def _map_hyperrag_status(raw: str) -> str | None:
    normalized = str(raw or "").strip().lower()
    if normalized in _HYPERRAG_INDEXED:
        return "indexed"
    if normalized in _HYPERRAG_FAILED:
        return "failed"
    if normalized in _HYPERRAG_RUNNING:
        return "indexing"
    return None


def _map_api_job_status(raw: str) -> str | None:
    normalized = str(raw or "").strip().lower()
    if normalized in _INDEX_DONE:
        return "indexed"
    if normalized in _INDEX_FAILED:
        return "failed"
    if normalized in _INDEX_RUNNING:
        return "indexing"
    return None


def _sync_hyperrag_terminal_to_catalog(
    library_id: str,
    asset_id: str,
    hyperrag_row: dict[str, Any],
    mapped: str,
) -> dict[str, Any] | None:
    job_id = str(hyperrag_row.get("id") or "").strip() or None
    engine_status = str(hyperrag_row.get("status") or "").strip().lower()
    error_message = hyperrag_row.get("error_message")

    if mapped == "indexed":
        version_id = _version_from_hyperrag_row(hyperrag_row)
        if job_id:
            store.upsert_index_job_snapshot(
                library_id,
                asset_id,
                job_id,
                "completed",
                error_message=None,
            )
        try:
            store.set_current_index_version_id(library_id, asset_id, version_id)
        except ValueError:
            pass
        asset = store.get_asset(library_id, asset_id)
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "indexed",
            "current_index_version_id": (asset.current_index_version_id if asset else None) or version_id,
            "job_id": None,
            "error_message": None,
        }

    if mapped == "failed":
        if job_id:
            store.upsert_index_job_snapshot(
                library_id,
                asset_id,
                job_id,
                engine_status or "failed",
                error_message=str(error_message) if error_message else None,
            )
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "failed",
            "current_index_version_id": None,
            "job_id": job_id,
            "error_message": error_message,
        }

    if mapped == "indexing" and job_id:
        store.upsert_index_job_snapshot(
            library_id,
            asset_id,
            job_id,
            engine_status or "running",
            error_message=str(error_message) if error_message else None,
        )
    return None


def _payload_from_persisted_job(library_id: str, asset_id: str, job: store.IndexJobRow) -> dict[str, Any]:
    mapped = _map_api_job_status(job.status)
    if mapped == "indexed":
        version_id = job.job_id
        try:
            store.set_current_index_version_id(library_id, asset_id, version_id)
        except ValueError:
            pass
        asset = store.get_asset(library_id, asset_id)
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "indexed",
            "current_index_version_id": (asset.current_index_version_id if asset else None) or version_id,
            "job_id": None,
            "error_message": None,
        }
    if mapped == "failed":
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "failed",
            "current_index_version_id": None,
            "job_id": job.job_id,
            "error_message": job.error_message,
        }
    if mapped == "indexing":
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "indexing",
            "current_index_version_id": None,
            "job_id": job.job_id,
            "error_message": job.error_message,
        }
    return {
        "library_id": library_id,
        "asset_id": asset_id,
        "status": "not_indexed",
        "current_index_version_id": None,
        "job_id": job.job_id,
        "error_message": job.error_message,
    }


def get_asset_index_status(library_id: str, asset_id: str) -> dict[str, Any]:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        raise KnowledgeError(422, {"detail": "library_id and asset_id are required"})

    asset = store.get_asset(library_id, asset_id)
    if asset is None:
        raise KnowledgeError(404, {"detail": "asset not found"})

    if asset.current_index_version_id:
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "indexed",
            "current_index_version_id": asset.current_index_version_id,
            "job_id": None,
            "error_message": None,
        }

    persisted = store.get_latest_index_job(library_id, asset_id)
    if persisted is not None:
        payload = _payload_from_persisted_job(library_id, asset_id, persisted)
        if payload["status"] != "not_indexed":
            return payload

    hyperrag_row = store.get_latest_hyperrag_asset_status(library_id, asset_id)
    if hyperrag_row is not None:
        mapped = _map_hyperrag_status(str(hyperrag_row.get("status") or ""))
        if mapped is not None:
            synced = _sync_hyperrag_terminal_to_catalog(library_id, asset_id, hyperrag_row, mapped)
            if synced is not None:
                return synced
            if mapped in {"failed", "indexing"}:
                return {
                    "library_id": library_id,
                    "asset_id": asset_id,
                    "status": mapped,
                    "current_index_version_id": None,
                    "job_id": str(hyperrag_row.get("id") or "") or None,
                    "error_message": hyperrag_row.get("error_message"),
                }

    redis = _redis_job_snapshot(library_id, asset_id)
    if redis is not None:
        if redis.get("job_id"):
            store.upsert_index_job_snapshot(
                library_id,
                asset_id,
                str(redis["job_id"]),
                str(redis.get("engine_status") or redis["status"]),
                error_message=str(redis["error_message"]) if redis.get("error_message") else None,
            )
        payload = {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": redis["status"],
            "current_index_version_id": None,
            "job_id": redis.get("job_id"),
            "error_message": redis.get("error_message"),
        }
        if redis["status"] == "indexed" and redis.get("job_id"):
            try:
                store.set_current_index_version_id(library_id, asset_id, str(redis["job_id"]))
            except ValueError:
                pass
            else:
                payload["current_index_version_id"] = str(redis["job_id"])
                payload["job_id"] = None
                payload["status"] = "indexed"
        return payload

    return {
        "library_id": library_id,
        "asset_id": asset_id,
        "status": "not_indexed",
        "current_index_version_id": None,
        "job_id": None,
        "error_message": None,
    }


def list_library_assets_index_status(library_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Index status for all assets in a library (one round-trip for the Playground table)."""
    library_id = str(library_id or "").strip()
    if not library_id:
        raise KnowledgeError(422, {"detail": "library_id is required"})
    if store.get_library(library_id) is None:
        raise KnowledgeError(404, {"detail": "library not found"})
    cap = 100 if limit is None else max(1, min(int(limit), 100))
    assets = store.list_assets(library_id, cap, None)
    return [get_asset_index_status(library_id, asset.asset_id) for asset in assets]


def get_latest_index_job(library_id: str, asset_id: str) -> dict[str, Any]:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        raise KnowledgeError(422, {"detail": "library_id and asset_id are required"})
    if store.get_asset(library_id, asset_id) is None:
        raise KnowledgeError(404, {"detail": "asset not found"})
    job = store.get_latest_index_job(library_id, asset_id)
    if job is None:
        raise KnowledgeError(404, {"detail": "index job not found"})
    return job.public()


def sync_index_job_from_api(job: dict[str, Any]) -> None:
    """Persist index job row from HyperRAG GET job; backfill catalog on terminal success."""
    job_id = str(job.get("job_id") or "").strip()
    library_id = str(job.get("library_id") or "").strip()
    asset_id = str(job.get("asset_id") or "").strip()
    status = str(job.get("status") or "").strip() or "pending"
    if not job_id or not library_id or not asset_id:
        return
    error_message = job.get("error_message")
    err = str(error_message) if error_message else None
    completed_at = job.get("completed_at")
    store.upsert_index_job_snapshot(
        library_id,
        asset_id,
        job_id,
        status,
        error_message=err,
        completed_at=completed_at,
    )
    persist_index_job_completion(job)


def persist_index_job_completion(job: dict[str, Any]) -> None:
    """Write catalog ``current_index_version_id`` when an index job reaches a terminal success state."""
    status = str(job.get("status") or "").strip().lower()
    if status not in _INDEX_DONE:
        return
    library_id = str(job.get("library_id") or "").strip()
    asset_id = str(job.get("asset_id") or "").strip()
    job_id = str(job.get("job_id") or "").strip()
    if not library_id or not asset_id:
        return
    version_id = job_id or f"indexed:{library_id}:{asset_id}"
    try:
        store.set_current_index_version_id(library_id, asset_id, version_id)
    except ValueError:
        return


__all__ = [
    "get_asset_index_status",
    "get_latest_index_job",
    "list_library_assets_index_status",
    "persist_index_job_completion",
    "sync_index_job_from_api",
]
