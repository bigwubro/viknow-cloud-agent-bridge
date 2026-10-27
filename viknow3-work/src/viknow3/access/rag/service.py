"""RAG Interface. Checks the request, then calls HyperragClient."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from viknow3.application.hyperrag.client import HYPERAG, HyperragClientError
from viknow3.application.hyperrag.object_gateway import ObjectFetchError, fetch_index_object, get_object_store

_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


class RagError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def upload_object(
    library_id: str,
    file_name: str,
    content: bytes,
    *,
    asset_id: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Store one source object. Creating the index is a later, separate call."""

    library = str(library_id or "").strip()
    if not library or "/" in library or "\\" in library or ".." in library or "\x00" in library:
        raise RagError(422, {"detail": [{"loc": ["library_id"], "msg": "invalid library_id", "type": "value_error"}]})
    name = PurePosixPath(str(file_name or "").replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise RagError(422, {"detail": [{"loc": ["file"], "msg": "file name is required", "type": "missing"}]})
    suffix = PurePosixPath(name).suffix.lower()
    if _SUFFIX.fullmatch(suffix) is None:
        raise RagError(422, {"detail": [{"loc": ["file"], "msg": "file extension is required", "type": "value_error"}]})
    if not content:
        raise RagError(422, {"detail": [{"loc": ["file"], "msg": "file is empty", "type": "value_error"}]})
    chosen = str(asset_id or "").strip() or str(uuid4())
    if _ASSET_ID.fullmatch(chosen) is None:
        raise RagError(422, {"detail": [{"loc": ["asset_id"], "msg": "invalid asset_id", "type": "value_error"}]})
    key = f"knowledge/assets/{chosen}/upload/{chosen}{suffix}"
    try:
        get_object_store().put_bytes(key, content, content_type=content_type or "application/octet-stream")
    except ObjectFetchError as exc:
        raise RagError(exc.status, exc.body) from exc
    return {
        "library_id": library,
        "asset_id": chosen,
        "asset_path": key,
        "file_name": name,
        "size_bytes": len(content),
    }


def _catalog_asset(library_id: str, asset_id: str, *, store: Any | None = None) -> Any:
    """The library row and the asset row must exist before an index job is submitted."""
    if store is None:
        from viknow3.access.knowledge import store as knowledge_store

        store = knowledge_store
    if store.get_library(library_id) is None:
        raise RagError(404, {"detail": "library not found"})
    asset = store.get_asset(library_id, asset_id)
    if asset is None:
        raise RagError(404, {"detail": "asset not found"})
    return asset


def create_index_job(body: dict[str, Any]) -> dict[str, Any]:
    library_id = str(body.get("library_id") or "").strip()
    asset_id = str(body.get("asset_id") or "").strip()
    asset_path = str(body.get("asset_path") or "").strip()
    if not library_id or not asset_id or not asset_path:
        raise RagError(422, {"detail": [{"loc": ["body"], "msg": "library_id, asset_id and asset_path are required", "type": "missing"}]})
    asset = _catalog_asset(library_id, asset_id)
    if asset_path != asset.asset_path:
        raise RagError(
            422,
            {"detail": [{"loc": ["body", "asset_path"], "msg": "asset_path does not match the asset", "type": "value_error"}]},
        )
    if asset_path.startswith("missing/") or "\x00" in asset_path:
        raise RagError(400, {"detail": "asset_path is not an available storage object key or absolute file path"})
    params = body.get("params") or {}
    title = params.get("title") if isinstance(params, dict) else None
    if title is None:
        title = asset.title
    if title is not None and not str(title).strip():
        raise RagError(422, {"detail": [{"loc": ["body", "params", "title"], "msg": "title must be non-empty", "type": "value_error"}]})
    compile_mode = body.get("compile_mode")
    if compile_mode not in (None, "pre_compile", "fast_ingest"):
        raise RagError(422, {"detail": [{"loc": ["body", "compile_mode"], "msg": "invalid compile_mode", "type": "literal_error"}]})
    mode = compile_mode if compile_mode in ("pre_compile", "fast_ingest") else None
    try:
        fetched = fetch_index_object(asset_path)
    except ObjectFetchError as exc:
        raise RagError(exc.status, exc.body) from exc
    job = HYPERAG.create_index(
        library_id,
        asset_id,
        str(fetched.path),
        str(title) if title else None,
        compile_mode=mode,
        object_key=fetched.key,
    )
    from viknow3.access.knowledge import store as knowledge_store

    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        knowledge_store.insert_index_job(
            library_id,
            asset_id,
            job_id,
            str(job.get("status") or "pending"),
            error_message=str(job.get("error_message")) if job.get("error_message") else None,
        )
    return job


def get_index_job(job_id: str) -> dict[str, Any]:
    if not job_id.strip():
        raise RagError(422, {"detail": "job_id is required"})
    job = _call(lambda: HYPERAG.get_index(job_id))
    from viknow3.access.knowledge.index_status import sync_index_job_from_api

    sync_index_job_from_api(job)
    return job


def get_index_job_tasks(job_id: str) -> list[dict[str, Any]]:
    if not job_id.strip():
        raise RagError(422, {"detail": "job_id is required"})
    return _call(lambda: HYPERAG.get_index_job_tasks(job_id))


def delete_index(library_id: str, asset_id: str) -> dict[str, Any]:
    if not library_id.strip() or not asset_id.strip():
        raise RagError(422, {"detail": [{"loc": ["body"], "msg": "library_id and asset_id are required", "type": "missing"}]})
    result = HYPERAG.delete_index(library_id, asset_id)
    from viknow3.access.knowledge import store as knowledge_store

    knowledge_store.clear_current_index_version_id(library_id.strip(), asset_id.strip())
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
        raise RagError(422, {"detail": errors})
    return HYPERAG.search(
        library_id,
        query,
        keywords if isinstance(keywords, list) else None,
        top_k=top_k,
        threshold=float(threshold),
    )


def asset_index(library_id: str, asset_id: str) -> dict[str, Any]:
    if not library_id.strip() or not asset_id.strip():
        raise RagError(400, {"detail": "library_id is required" if not library_id.strip() else "asset_id is required"})
    if "/" in library_id or "\\" in library_id or ".." in library_id or "/" in asset_id or "\\" in asset_id or ".." in asset_id:
        raise RagError(400, {"detail": "invalid library_id" if "/" in library_id or ".." in library_id else "invalid asset_id"})
    return _call(lambda: HYPERAG.asset_index(library_id, asset_id))


def _call(action):
    try:
        return action()
    except HyperragClientError as exc:
        raise RagError(exc.status, exc.body) from exc
