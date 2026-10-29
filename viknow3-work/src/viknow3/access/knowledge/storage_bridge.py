"""Knowledge layer access to viknow-storage (no direct MinIO/S3 from viknow3)."""

from __future__ import annotations

import re
from typing import Any

from viknow3.service.storage.client import StorageClientError
from viknow3.service.storage.gateway import get_storage_client, storage_configured

_ASSET_PATH = re.compile(
    r"^knowledge/assets/(?P<asset_id>[a-zA-Z0-9][a-zA-Z0-9._-]{0,127})/upload/.+"
)


def require_storage() -> None:
    if not storage_configured():
        raise RuntimeError("viknow-storage is not configured (VIKNOW_STORAGE_URL + VIKNOW_STORAGE_TOKEN)")


def parse_asset_id_from_path(asset_path: str) -> str | None:
    match = _ASSET_PATH.match(str(asset_path or "").strip())
    return match.group("asset_id") if match else None


def asset_exists_in_storage(library_id: str, asset_id: str) -> None:
    require_storage()
    client = get_storage_client()
    try:
        if not client.asset_exists(library_id, asset_id):
            raise FileNotFoundError("asset not found in storage")
    except StorageClientError as exc:
        raise RuntimeError(str(exc.detail)) from exc


def upload_bytes(library_id: str, file_name: str, payload: bytes, *, asset_id: str, content_type: str) -> None:
    require_storage()
    client = get_storage_client()
    try:
        client.upload_asset(
            library_id,
            file_name,
            payload,
            content_type=content_type or "application/octet-stream",
            asset_id=asset_id,
        )
    except StorageClientError as exc:
        raise RuntimeError(str(exc.detail)) from exc


def download_bytes(library_id: str, asset_id: str) -> bytes:
    require_storage()
    client = get_storage_client()
    try:
        return client.download(library_id, asset_id)
    except StorageClientError as exc:
        raise RuntimeError(str(exc.detail)) from exc


def fetch_preview(library_id: str, asset_id: str) -> tuple[bytes, str]:
    require_storage()
    client = get_storage_client()
    try:
        response = client._http.get(f"/libraries/{library_id}/assets/{asset_id}/preview")
        client._raise(response)
        ctype = response.headers.get("content-type") or "application/octet-stream"
        return response.content, ctype
    except StorageClientError as exc:
        raise RuntimeError(str(exc.detail)) from exc


def storage_error_to_http(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, StorageClientError):
        detail = exc.detail
        if isinstance(detail, dict):
            return exc.status, str(detail.get("detail") or detail)
        return exc.status, str(detail)
    if isinstance(exc, FileNotFoundError):
        return 404, "asset not found in storage"
    return 503, str(exc)
