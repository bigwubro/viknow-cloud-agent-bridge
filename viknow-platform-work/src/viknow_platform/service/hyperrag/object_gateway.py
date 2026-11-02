"""Materialize viknow-storage assets onto scratch for HyperRAG indexing."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRATCH_DIR_ENV = "VIKNOW_SCRATCH_DIR"


class ObjectFetchError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


@dataclass(frozen=True)
class IndexObject:
    key: str
    path: Path


def _index_suffix(filename: str | None) -> str:
    for candidate in (filename,):
        if not candidate:
            continue
        suffix = Path(str(candidate)).suffix.lower()
        if suffix:
            return suffix
    return ""


def fetch_index_asset(library_id: str, asset_id: str, *, filename: str | None = None) -> IndexObject:
    """Download bytes from viknow-storage into scratch for HyperRAG indexing."""

    from viknow_platform.service.storage.client import StorageClientError
    from viknow_platform.service.storage.gateway import get_storage_client

    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    if not library or not asset:
        raise ObjectFetchError(422, {"detail": "library_id and asset_id are required"})
    key = f"storage://{library}/{asset}"
    resolved_name = str(filename).strip() if filename is not None else ""
    client = None
    if not resolved_name:
        client = get_storage_client()
        try:
            meta = client.get_asset(library, asset)
            resolved_name = str(meta.get("filename") or meta.get("title") or "")
        except StorageClientError as exc:
            raise ObjectFetchError(exc.status, {"detail": exc.detail}) from exc
    suffix = _index_suffix(resolved_name)
    if not suffix:
        raise ObjectFetchError(
            422,
            {"detail": "asset filename must include a file extension supported for indexing"},
        )
    if client is None:
        client = get_storage_client()
    path = scratch_root() / "storage" / library / asset / f"original{suffix}"
    locks = scratch_root() / ".materialize-locks"
    locks.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(f"{key}:{path}".encode()).hexdigest()
    with (locks / identity).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file() and path.stat().st_size > 0:
            return IndexObject(key, path)
        try:
            payload = client.download(library, asset)
        except StorageClientError as exc:
            raise ObjectFetchError(exc.status, {"detail": exc.detail}) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ObjectFetchError(503, {"detail": "object storage is unavailable"})
    return IndexObject(key, path)


def _default_scratch_dir() -> Path:
    return Path.home() / ".cache" / "viknow-platform" / "scratch"


def scratch_root() -> Path:
    configured = os.environ.get(SCRATCH_DIR_ENV, "").strip()
    root = Path(configured).expanduser() if configured else _default_scratch_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if configured:
            raise
        fallback = Path(tempfile.gettempdir()) / f"viknow-scratch-{os.getuid()}"
        fallback.mkdir(parents=True, exist_ok=True)
        root = fallback
    return root
