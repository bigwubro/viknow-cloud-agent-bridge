"""Mint time-limited download URLs from viknow-storage for index jobs."""

from __future__ import annotations

from pathlib import Path

from viknow_platform.service.storage.client import StorageClientError
from viknow_platform.service.storage.gateway import get_storage_client, storage_configured

DEFAULT_INDEX_DOWNLOAD_TTL_SECONDS = 86400

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v"}


class IndexDownloadUrlError(Exception):
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body


def storage_canonical_uri(library_id: str, asset_id: str, *, filename: str | None = None) -> str:
    base = f"storage://{library_id}/{asset_id}"
    if not filename:
        return base
    suffix = Path(str(filename)).suffix.lower()
    if suffix and suffix not in base:
        return f"{base}{suffix}"
    return base


def is_video_filename(name: str | None) -> bool:
    if not name:
        return False
    return Path(str(name)).suffix.lower() in _VIDEO_SUFFIXES


def mint_index_download_url(library_id: str, asset_id: str, *, ttl_seconds: int = DEFAULT_INDEX_DOWNLOAD_TTL_SECONDS) -> dict:
    if not storage_configured():
        raise IndexDownloadUrlError(503, {"detail": "viknow-storage is not configured"})
    client = get_storage_client()
    try:
        return client.mint_download_url(library_id, asset_id, ttl_seconds=ttl_seconds)
    except StorageClientError as exc:
        raise IndexDownloadUrlError(exc.status, {"detail": exc.detail}) from exc
