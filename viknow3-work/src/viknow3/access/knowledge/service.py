"""Knowledge index, status, and search (catalog blobs are in viknow-storage)."""

from __future__ import annotations

from typing import Any

from viknow3.access.knowledge import store
from viknow3.service.hyperrag.client import HYPERAG, HyperragClientError
from viknow3.service.hyperrag.object_gateway import ObjectFetchError, fetch_index_asset
from viknow3.service.storage.client import StorageClientError
from viknow3.service.storage.gateway import get_storage_client, storage_configured


class KnowledgeError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def get_asset_index_status(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_asset_index_status as _get

    return _get(library_id, asset_id)


def list_library_assets_index_status(library_id: str, limit: int | None) -> list[dict[str, Any]]:
    from viknow3.access.knowledge.index_status import list_library_assets_index_status as _list

    return _list(library_id, limit)


def get_latest_asset_index_job(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_latest_index_job as _get

    return _get(library_id, asset_id)


def create_index_job(body: dict[str, Any]) -> dict[str, Any]:
    if not storage_configured():
        raise KnowledgeError(503, {"detail": "viknow-storage is required for index jobs"})
    library_id = str(body.get("library_id") or "").strip()
    asset_id = str(body.get("asset_id") or "").strip()
    if body.get("asset_path"):
        raise KnowledgeError(
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "asset_path"],
                        "msg": "asset_path is no longer accepted; upload to viknow-storage",
                        "type": "extra_forbidden",
                    }
                ]
            },
        )
    if not library_id or not asset_id:
        raise KnowledgeError(
            422,
            {"detail": [{"loc": ["body"], "msg": "library_id and asset_id are required", "type": "missing"}]},
        )
    params = body.get("params") or {}
    title = params.get("title") if isinstance(params, dict) else None
    try:
        client = get_storage_client()
        if not client.asset_exists(library_id, asset_id):
            raise KnowledgeError(404, {"detail": "asset not found"})
        remote = client.get_asset(library_id, asset_id)
    except StorageClientError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc
    store.ensure_asset_for_index(
        library_id,
        asset_id,
        str(remote.get("title") or asset_id),
        str(remote.get("modality") or "document"),
    )
    if title is None:
        title = remote.get("title")

    if title is not None and not str(title).strip():
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "params", "title"], "msg": "title must be non-empty", "type": "value_error"}]})
    compile_mode = body.get("compile_mode")
    if compile_mode not in (None, "pre_compile", "fast_ingest"):
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "compile_mode"], "msg": "invalid compile_mode", "type": "literal_error"}]})
    mode = compile_mode if compile_mode in ("pre_compile", "fast_ingest") else None
    try:
        fetched = fetch_index_asset(library_id, asset_id)
    except ObjectFetchError as exc:
        raise KnowledgeError(exc.status, exc.body) from exc
    job = HYPERAG.create_index(
        library_id,
        asset_id,
        str(fetched.path),
        str(title) if title else None,
        compile_mode=mode,
        object_key=fetched.key,
    )
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        store.insert_index_job(
            library_id,
            asset_id,
            job_id,
            str(job.get("status") or "pending"),
            error_message=str(job.get("error_message")) if job.get("error_message") else None,
        )
    return job


def get_index_job(job_id: str) -> dict[str, Any]:
    if not job_id.strip():
        raise KnowledgeError(422, {"detail": "job_id is required"})
    job = _hyperrag_call(lambda: HYPERAG.get_index(job_id))
    from viknow3.access.knowledge.index_status import sync_index_job_from_api

    sync_index_job_from_api(job)
    return job


def get_index_job_tasks(job_id: str) -> list[dict[str, Any]]:
    if not job_id.strip():
        raise KnowledgeError(422, {"detail": "job_id is required"})
    return _hyperrag_call(lambda: HYPERAG.get_index_job_tasks(job_id))


def delete_index(library_id: str, asset_id: str) -> dict[str, Any]:
    if not library_id.strip() or not asset_id.strip():
        raise KnowledgeError(422, {"detail": [{"loc": ["body"], "msg": "library_id and asset_id are required", "type": "missing"}]})
    result = HYPERAG.delete_index(library_id, asset_id)
    store.clear_current_index_version_id(library_id.strip(), asset_id.strip())
    return result


def search(body: dict[str, Any]) -> dict[str, Any]:
    library_id = str(body.get("library_id") or "").strip()
    query = str(body.get("query") or "").strip()
    errors = []
    if not library_id:
        errors.append({"loc": ["body", "library_id"], "msg": "field required", "type": "missing"})
    if not query:
        errors.append({"loc": ["body", "query"], "msg": "field required", "type": "missing"})
    keywords = body.get("bm25_keywords")
    if keywords is not None:
        if not isinstance(keywords, list) or not keywords or any(not str(item).strip() for item in keywords):
            errors.append({"loc": ["body", "bm25_keywords"], "msg": "keywords must be a non-empty list of strings", "type": "value_error"})
    top_k = body.get("top_k", 10)
    threshold = body.get("threshold", 0.3)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        errors.append({"loc": ["body", "top_k"], "msg": "top_k must be >= 1", "type": "greater_than_equal"})
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= float(threshold) <= 1:
        errors.append({"loc": ["body", "threshold"], "msg": "threshold must be between 0 and 1", "type": "value_error"})
    if errors:
        raise KnowledgeError(422, {"detail": errors})
    return HYPERAG.search(
        library_id,
        query,
        keywords if isinstance(keywords, list) else None,
        top_k=top_k,
        threshold=float(threshold),
    )


def asset_index(library_id: str, asset_id: str) -> dict[str, Any]:
    if not library_id.strip() or not asset_id.strip():
        raise KnowledgeError(400, {"detail": "library_id is required" if not library_id.strip() else "asset_id is required"})
    if "/" in library_id or "\\" in library_id or ".." in library_id or "/" in asset_id or "\\" in asset_id or ".." in asset_id:
        raise KnowledgeError(400, {"detail": "invalid library_id" if "/" in library_id or ".." in library_id else "invalid asset_id"})
    return _hyperrag_call(lambda: HYPERAG.asset_index(library_id, asset_id))


def _hyperrag_call(action):
    try:
        return action()
    except HyperragClientError as exc:
        raise KnowledgeError(exc.status, exc.body) from exc
