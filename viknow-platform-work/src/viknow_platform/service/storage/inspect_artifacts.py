"""Persist document_inspect rendered pages under viknow-storage asset artifacts."""

from __future__ import annotations

from urllib.parse import quote

from viknow_platform.service.storage.client import StorageClientError
from viknow_platform.service.storage.gateway import get_storage_client, storage_configured


def inspect_page_artifact_id(page_number: int) -> str:
    if page_number < 1:
        raise ValueError("page_number must be at least 1")
    return f"inspect.page-{page_number:04d}.png"


def artifact_storage_uri(library_id: str, asset_id: str, artifact_id: str) -> str:
    base = f"storage://{library_id}/{asset_id}"
    return f"{base}?artifact={quote(artifact_id, safe='')}"


def upload_inspect_page_artifact(
    library_id: str,
    asset_id: str,
    page_number: int,
    png_bytes: bytes,
) -> str:
    """Upload one inspect page PNG; returns ``storage://…?artifact=inspect.page-….png``."""
    if not storage_configured():
        raise RuntimeError("viknow-storage is not configured")
    artifact_id = inspect_page_artifact_id(page_number)
    client = get_storage_client()
    _upload_artifact_replace(client, library_id, asset_id, artifact_id, png_bytes)
    return artifact_storage_uri(library_id, asset_id, artifact_id)


def _upload_artifact_replace(
    client,
    library_id: str,
    asset_id: str,
    artifact_id: str,
    content: bytes,
) -> None:
    filename = artifact_id
    try:
        client.upload_artifact(
            library_id,
            asset_id,
            artifact_id=artifact_id,
            filename=filename,
            content=content,
            content_type="image/png",
        )
    except StorageClientError as exc:
        if exc.status != 409:
            raise
        client.delete_artifact(library_id, asset_id, artifact_id)
        client.upload_artifact(
            library_id,
            asset_id,
            artifact_id=artifact_id,
            filename=filename,
            content=content,
            content_type="image/png",
        )
