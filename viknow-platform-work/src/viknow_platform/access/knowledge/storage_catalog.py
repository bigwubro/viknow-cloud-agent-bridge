"""Read library/asset catalog from viknow-storage (authoritative; not platform Postgres)."""

from __future__ import annotations

from typing import Any

from viknow_platform.access.knowledge.service import KnowledgeError
from viknow_platform.service.storage.client import StorageClientError
from viknow_platform.service.storage.gateway import get_storage_client, storage_configured


def require_storage_configured() -> None:
    if not storage_configured():
        raise KnowledgeError(503, {"detail": "viknow-storage is not configured"})


def _client():
    require_storage_configured()
    return get_storage_client()


def library_exists(library_id: str) -> bool:
    library_id = str(library_id or "").strip()
    if not library_id:
        return False
    try:
        _client().get_library(library_id)
        return True
    except StorageClientError as exc:
        if exc.status == 404:
            return False
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc


def require_library(library_id: str) -> None:
    if not library_exists(library_id):
        raise KnowledgeError(404, {"detail": "library not found"})


def require_asset(library_id: str, asset_id: str) -> dict[str, Any]:
    library_id = str(library_id or "").strip()
    asset_id = str(asset_id or "").strip()
    if not library_id or not asset_id:
        raise KnowledgeError(422, {"detail": "library_id and asset_id are required"})
    try:
        return _client().get_asset(library_id, asset_id)
    except StorageClientError as exc:
        if exc.status == 404:
            raise KnowledgeError(404, {"detail": "asset not found"}) from exc
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc


def list_assets(library_id: str, limit: int, after: str | None = None) -> list[dict[str, Any]]:
    require_library(library_id)
    try:
        rows = _client().list_assets(library_id, limit=limit, after=after)
    except StorageClientError as exc:
        raise KnowledgeError(exc.status, {"detail": exc.detail}) from exc
    return rows if isinstance(rows, list) else []
