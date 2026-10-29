"""Mint time-limited download URLs from viknow-storage for index jobs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse, urlunparse

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


def rewrite_grant_content_url_for_actio_fetch(url: str, *, fetch_base: str | None = None) -> str:
    """Replace grant URL host with a base reachable from Actio on 236 (e.g. nginx :5220 on docker bridge)."""
    import os

    base = (fetch_base or os.environ.get("VIKNOW_STORAGE_GRANT_FETCH_BASE", "")).strip().rstrip("/")
    if not base:
        return str(url or "").strip()
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or "/libraries/" not in parsed.path:
        return str(url or "").strip()
    target = urlparse(base)
    return urlunparse((target.scheme, target.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def mint_index_download_url(library_id: str, asset_id: str, *, ttl_seconds: int = DEFAULT_INDEX_DOWNLOAD_TTL_SECONDS) -> dict:
    if not storage_configured():
        raise IndexDownloadUrlError(503, {"detail": "viknow-storage is not configured"})
    client = get_storage_client()
    try:
        issued = client.mint_download_url(library_id, asset_id, ttl_seconds=ttl_seconds)
    except StorageClientError as exc:
        raise IndexDownloadUrlError(exc.status, {"detail": exc.detail}) from exc
    url = str(issued.get("url") or "").strip()
    if url:
        issued = dict(issued)
        issued["url"] = rewrite_grant_content_url_for_actio_fetch(url)
    return issued
