"""Knowledge index status service (catalog HTTP lives on viknow-storage)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class KnowledgeError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


def get_asset_index_status(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow_platform.access.knowledge.index_status import get_asset_index_status as _get

    return _get(library_id, asset_id)


def list_library_assets_index_status(library_id: str, limit: int | None) -> list[dict[str, Any]]:
    from viknow_platform.access.knowledge.index_status import list_library_assets_index_status as _list

    return _list(library_id, limit)


def get_latest_asset_index_job(library_id: str, asset_id: str) -> dict[str, Any]:
    from viknow_platform.access.knowledge.index_status import get_latest_index_job as _get

    return _get(library_id, asset_id)


_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm"})


def build_asset_path(asset_id: str, file_name: str) -> str:
    """Legacy path shape for HyperRAG callers; indexing uses viknow-storage bytes."""

    suffix = Path(file_name).suffix.lower()
    if not suffix:
        raise KnowledgeError(422, {"detail": "file extension is required"})
    if suffix in _VIDEO_SUFFIXES:
        upload_name = f"{asset_id}.normalized{suffix}"
    else:
        upload_name = f"{asset_id}{suffix}"
    return f"knowledge/assets/{asset_id}/upload/{upload_name}"
