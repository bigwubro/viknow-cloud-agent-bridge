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


def fetch_index_asset(library_id: str, asset_id: str) -> IndexObject:
    """Download bytes from viknow-storage into scratch for HyperRAG indexing."""

    from viknow3.service.storage.client import StorageClientError
    from viknow3.service.storage.gateway import get_storage_client

    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    if not library or not asset:
        raise ObjectFetchError(422, {"detail": "library_id and asset_id are required"})
    key = f"storage://{library}/{asset}"
    path = scratch_root() / "storage" / library / asset / "original"
    locks = scratch_root() / ".materialize-locks"
    locks.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(f"{key}:{path}".encode()).hexdigest()
    with (locks / identity).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file() and path.stat().st_size > 0:
            return IndexObject(key, path)
        try:
            payload = get_storage_client().download(library, asset)
        except StorageClientError as exc:
            raise ObjectFetchError(exc.status, {"detail": exc.detail}) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ObjectFetchError(503, {"detail": "object storage is unavailable"})
    return IndexObject(key, path)


def _default_scratch_dir() -> Path:
    return Path.home() / ".cache" / "viknow3" / "scratch"


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
