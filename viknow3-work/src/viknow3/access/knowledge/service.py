"""Knowledge library / asset service."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from viknow3.access.knowledge import storage_bridge, store
from viknow3.service.storage.gateway import get_storage_client, storage_configured

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
    if storage_configured():
        try:
            get_storage_client().create_library(library_id, title_text)
        except Exception:
            pass
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
        storage_bridge.asset_exists_in_storage(library_id, asset_id)
    except Exception as exc:
        status, detail = storage_bridge.storage_error_to_http(exc)
        raise KnowledgeError(status, {"detail": detail}) from exc
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
    """Canonical catalog path (storage is keyed by library_id + asset_id)."""

    suffix = Path(file_name).suffix.lower()
    if not suffix:
        raise KnowledgeError(422, {"detail": "file extension is required"})
    if suffix in _VIDEO_SUFFIXES:
        upload_name = f"{asset_id}.normalized{suffix}"
    else:
        upload_name = f"{asset_id}{suffix}"
    return f"knowledge/assets/{asset_id}/upload/{upload_name}"


def upload_library_file(library_id: str, file_name: str, stream: Any, content_type: str) -> dict[str, Any]:
    """Multipart upload: API writes to viknow-storage (browser never talks to MinIO)."""

    get_library(library_id)
    name = str(file_name or "").strip()
    if not name:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file name is required", "type": "missing"}]})
    aid = str(uuid4())
    path = build_asset_path(aid, name)
    payload = stream.read()
    if not payload:
        raise KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file is empty", "type": "value_error"}]})
    try:
        storage_bridge.upload_bytes(library_id, name, payload, asset_id=aid, content_type=content_type or "application/octet-stream")
    except Exception as exc:
        status, detail = storage_bridge.storage_error_to_http(exc)
        raise KnowledgeError(status, {"detail": detail}) from exc
    return {
        "asset_id": aid,
        "asset_path": path,
        "library_id": library_id,
        "content_type": content_type or "application/octet-stream",
    }


def stream_asset_preview(library_id: str, asset_id: str) -> tuple[bytes, str]:
    get_library(library_id)
    if store.get_asset(library_id, asset_id) is None:
        raise KnowledgeError(404, {"detail": "asset not found"})
    try:
        return storage_bridge.fetch_preview(library_id, asset_id)
    except Exception as exc:
        status, detail = storage_bridge.storage_error_to_http(exc)
        raise KnowledgeError(status, {"detail": detail}) from exc


def stream_asset_content(library_id: str, asset_id: str) -> tuple[bytes, str]:
    get_library(library_id)
    asset = store.get_asset(library_id, asset_id)
    if asset is None:
        raise KnowledgeError(404, {"detail": "asset not found"})
    try:
        payload = storage_bridge.download_bytes(library_id, asset_id)
    except Exception as exc:
        status, detail = storage_bridge.storage_error_to_http(exc)
        raise KnowledgeError(status, {"detail": detail}) from exc
    suffix = Path(asset.asset_path).suffix.lower()
    ctype = "application/octet-stream"
    if suffix == ".pdf":
        ctype = "application/pdf"
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        ctype = f"image/{suffix.lstrip('.')}"
    elif suffix in _VIDEO_SUFFIXES:
        ctype = f"video/{suffix.lstrip('.')}"
    return payload, ctype


def get_asset_index_status(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_asset_index_status as _get

    return _get(library_id, asset_id)


def list_library_assets_index_status(library_id: str, limit: int | None) -> list[dict[str, Any]]:
    from viknow3.access.knowledge.index_status import list_library_assets_index_status as _list

    return _list(library_id, limit)


def get_latest_asset_index_job(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow3.access.knowledge.index_status import get_latest_index_job as _get

    return _get(library_id, asset_id)
