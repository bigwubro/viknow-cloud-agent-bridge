"""Knowledge library, asset, index, and search service."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from viknow3.access.knowledge import store
from viknow3.service.hyperrag.client import HYPERAG, HyperragClientError
from viknow3.service.hyperrag.object_gateway import ObjectFetchError, fetch_index_asset, fetch_index_object, get_object_store
from viknow3.service.storage.client import StorageClientError
from viknow3.service.storage.gateway import get_storage_client, storage_configured
from viknow3.storage import s3

_LIBRARY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_ASSET_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

_MODALITY = {
    ".pdf": "document",
    ".txt": "document",
    ".md": "document",
    ".markdown": "document",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


class KnowledgeError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def _limit(value: int | None) -> int:
    if value is None:
        return 20
    return max(1, min(value, 100))


def _modality(name: str, given: str | None) -> str:
    if given:
        return given.strip()
    suffix = Path(name).suffix.lower()
    return _MODALITY.get(suffix, "document")


def create_library(body: dict[str, Any]) -> dict[str, Any]:
    library_id = str(body.get("library_id") or "").strip()
    if not library_id or not _LIBRARY_ID.match(library_id):
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "library_id"], "msg": "invalid library_id", "type": "value_error"}]})
    title = body.get("title")
    title_text = str(title).strip() if title is not None else library_id
    try:
        row = store.create_library(library_id, title_text)
    except ValueError as exc:
        if str(exc) == "library_exists":
            raise KnowledgeError(409, {"detail": "library_exists"}) from exc
        raise
    return row.public()


def list_libraries(limit: int | None, after: str | None) -> list[dict[str, Any]]:
    rows = store.list_libraries(_limit(limit), after)
    return [row.public() for row in rows]


def get_library(library_id: str) -> dict[str, Any]:
    row = store.get_library(library_id)
    if row is None:
        raise KnowledgeError(404, {"detail": "library not found"})
    return row.public()


def create_asset(library_id: str, body: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(body.get("asset_id") or "").strip()
    asset_path = str(body.get("asset_path") or "").strip()
    if not asset_id or not _ASSET_ID.match(asset_id):
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "asset_id"], "msg": "field required", "type": "missing"}]})
    if not asset_path:
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "asset_path"], "msg": "field required", "type": "missing"}]})
    try:
        s3.head_object(asset_path)
    except s3.StorageError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc
    title = body.get("title")
    file_hint = title or asset_id or asset_path
    modality = _modality(str(file_hint), body.get("modality"))
    title_text = str(title).strip() if title else Path(asset_path).name
    try:
        row = store.create_asset(library_id, asset_id, title_text, modality, asset_path)
    except ValueError as exc:
        code = str(exc)
        if code == "library_not_found":
            raise KnowledgeError(404, {"detail": "library not found"}) from exc
        if code == "asset_exists":
            raise KnowledgeError(409, {"detail": "asset_exists"}) from exc
        raise
    return row.public()


def list_assets(library_id: str, limit: int | None, after: str | None) -> list[dict[str, Any]]:
    try:
        rows = store.list_assets(library_id, _limit(limit), after)
    except ValueError as exc:
        if str(exc) == "library_not_found":
            raise KnowledgeError(404, {"detail": "library not found"}) from exc
        raise
    return [row.public() for row in rows]


_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm"})


def build_asset_path(asset_id: str, file_name: str) -> str:
    """Canonical object key: knowledge/assets/{asset_id}/upload/{asset_id}[.normalized].{ext}"""

    suffix = Path(file_name).suffix.lower()
    if not suffix:
        raise KnowledgeError(422, {"detail": "file extension is required"})
    if suffix in _VIDEO_SUFFIXES:
        upload_name = f"{asset_id}.normalized{suffix}"
    else:
        upload_name = f"{asset_id}{suffix}"
    return f"knowledge/assets/{asset_id}/upload/{upload_name}"


def upload_library_file(library_id: str, file_name: str, stream: Any, content_type: str) -> dict[str, Any]:
    """Store bytes on the object gateway from the Playground BFF (avoids browser CORS to MinIO)."""

    get_library(library_id)
    name = str(file_name or "").strip()
    if not name:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file name is required", "type": "missing"}]})
    aid = str(uuid4())
    path = build_asset_path(aid, name)
    try:
        key = s3.put_object_stream(path, stream, content_type or "application/octet-stream")
    except s3.StorageError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc
    return {
        "asset_id": aid,
        "asset_path": key,
        "library_id": library_id,
        "content_type": content_type or "application/octet-stream",
    }


def presign_upload(library_id: str, file_name: str, asset_id: str | None, content_type: str) -> dict[str, Any]:
    aid = (asset_id or "").strip() or str(uuid4())
    if not _ASSET_ID.match(aid):
        raise KnowledgeError(422, {"detail": "invalid asset_id"})
    path = build_asset_path(aid, file_name)
    try:
        signed = s3.presign_put(path, content_type or "application/octet-stream")
    except s3.StorageError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc
    signed["asset_id"] = aid
    signed["library_id"] = library_id
    return signed


def presign_download(asset_path: str, download_name: str | None) -> dict[str, Any]:
    try:
        return s3.presign_get(asset_path, download_name=download_name)
    except s3.StorageError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc


def get_asset_index_status(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_asset_index_status as _get

    return _get(library_id, asset_id)


def list_library_assets_index_status(library_id: str, limit: int | None) -> list[dict[str, Any]]:
    from viknow3.access.knowledge.index_status import list_library_assets_index_status as _list

    return _list(library_id, limit)


def get_latest_asset_index_job(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_latest_index_job as _get

    return _get(library_id, asset_id)


_UPLOAD_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UPLOAD_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


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
        raise KnowledgeError(422, {"detail": [{"loc": ["library_id"], "msg": "invalid library_id", "type": "value_error"}]})
    name = PurePosixPath(str(file_name or "").replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file name is required", "type": "missing"}]})
    suffix = PurePosixPath(name).suffix.lower()
    if _UPLOAD_SUFFIX.fullmatch(suffix) is None:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file extension is required", "type": "value_error"}]})
    if not content:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file is empty", "type": "value_error"}]})
    chosen = str(asset_id or "").strip() or str(uuid4())
    if _UPLOAD_ASSET_ID.fullmatch(chosen) is None:
        raise KnowledgeError(422, {"detail": [{"loc": ["asset_id"], "msg": "invalid asset_id", "type": "value_error"}]})
    key = f"knowledge/assets/{chosen}/upload/{chosen}{suffix}"
    try:
        get_object_store().put_bytes(key, content, content_type=content_type or "application/octet-stream")
    except ObjectFetchError as exc:
        raise KnowledgeError(exc.status, exc.body) from exc
    return {
        "library_id": library,
        "asset_id": chosen,
        "asset_path": key,
        "file_name": name,
        "size_bytes": len(content),
    }


def _catalog_asset(library_id: str, asset_id: str, *, store: Any | None = None) -> Any:
    """The library row and the asset row must exist before an index job is submitted."""
    catalog = store
    if catalog is None:
        from viknow3.access.knowledge import store as catalog
    if catalog.get_library(library_id) is None:
        raise KnowledgeError(404, {"detail": "library not found"})
    asset = catalog.get_asset(library_id, asset_id)
    if asset is None:
        raise KnowledgeError(404, {"detail": "asset not found"})
    return asset


def create_index_job(body: dict[str, Any]) -> dict[str, Any]:
    library_id = str(body.get("library_id") or "").strip()
    asset_id = str(body.get("asset_id") or "").strip()
    asset_path = str(body.get("asset_path") or "").strip()
    if not library_id or not asset_id:
        raise KnowledgeError(
            422,
            {"detail": [{"loc": ["body"], "msg": "library_id and asset_id are required", "type": "missing"}]},
        )
    params = body.get("params") or {}
    title = params.get("title") if isinstance(params, dict) else None

    if asset_path:
        asset = _catalog_asset(library_id, asset_id)
        if asset_path != asset.asset_path:
            raise KnowledgeError(
                422,
                {"detail": [{"loc": ["body", "asset_path"], "msg": "asset_path does not match the asset", "type": "value_error"}]},
            )
        if asset_path.startswith("missing/") or "\x00" in asset_path:
            raise KnowledgeError(400, {"detail": "asset_path is not an available storage object key or absolute file path"})
        if title is None:
            title = asset.title
        fetch = lambda: fetch_index_object(asset_path)
    elif storage_configured():
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
        fetch = lambda: fetch_index_asset(library_id, asset_id)
    else:
        raise KnowledgeError(
            422,
            {"detail": [{"loc": ["body", "asset_path"], "msg": "asset_path is required when viknow-storage is not configured", "type": "missing"}]},
        )

    if title is not None and not str(title).strip():
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "params", "title"], "msg": "title must be non-empty", "type": "value_error"}]})
    compile_mode = body.get("compile_mode")
    if compile_mode not in (None, "pre_compile", "fast_ingest"):
        raise KnowledgeError(422, {"detail": [{"loc": ["body", "compile_mode"], "msg": "invalid compile_mode", "type": "literal_error"}]})
    mode = compile_mode if compile_mode in ("pre_compile", "fast_ingest") else None
    try:
        fetched = fetch()
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


def proxy_storage_asset(
    library_id: str,
    asset_id: str,
    *,
    preview: bool,
    range_header: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Stream bytes from viknow-storage for Playground/agents (AppKey on this hop)."""

    if not storage_configured():
        raise KnowledgeError(503, {"detail": "viknow-storage is not configured"})
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        raise KnowledgeError(422, {"detail": "library_id and asset_id are required"})
    try:
        client = get_storage_client()
        if not client.asset_exists(library_id, asset_id):
            raise KnowledgeError(404, {"detail": "asset not found"})
        return client.fetch_bytes(library_id, asset_id, preview=preview, range_header=range_header)
    except StorageClientError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc


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
