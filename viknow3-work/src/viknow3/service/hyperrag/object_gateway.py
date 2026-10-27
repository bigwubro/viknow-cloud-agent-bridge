"""Fetch a knowledge object from the S3 gateway onto scratch for indexing.

The gateway object is the durable copy. ``aadd_asset`` reads a POSIX path, so
the index call downloads the key into scratch and passes that local file.
The job still records the object key. This module does not upload source
files or derived index artifacts.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlparse

from viknow3.ops.config.store import ConfigError, plain_singleton

UNAVAILABLE = "asset_path is not an available storage object key or absolute file path"
SCRATCH_DIR_ENV = "VIKNOW_SCRATCH_DIR"
# Legacy shared path; avoid defaulting here because other users may own `/tmp/viknow-scratch`.
DEFAULT_SCRATCH_DIR = "/tmp/viknow-scratch"
_KNOWLEDGE_PREFIX = "knowledge/"


class ObjectFetchError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body


@dataclass(frozen=True)
class IndexObject:
    key: str
    path: Path


class ObjectStore(Protocol):
    def exists(self, key: str) -> bool: ...

    def get_to_path(self, key: str, path: Path) -> None: ...

    def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None: ...


class GatewayStore:
    """Path-style client for the one bucket named by ``s3_client``."""

    def __init__(self, client: Any, bucket: str) -> None:
        cleaned = str(bucket or "").strip()
        if not cleaned or "/" in cleaned:
            raise ObjectFetchError(503, {"detail": "s3_client bucket is invalid"})
        self._client = client
        self.bucket = cleaned

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self.bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise ObjectFetchError(503, {"detail": "object storage is unavailable"}) from exc
        return True

    def get_to_path(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.fget_object(self.bucket, key, str(path))

    def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
        import io

        from minio.error import S3Error

        try:
            self._client.put_object(
                self.bucket,
                key,
                io.BytesIO(payload),
                length=len(payload),
                content_type=content_type,
            )
        except S3Error as exc:
            raise ObjectFetchError(503, {"detail": "object storage is unavailable"}) from exc


_override: ObjectStore | None = None
_cached: ObjectStore | None = None


def install_object_store(store: ObjectStore | None) -> None:
    """Tests inject a store. Production leaves this unset."""

    global _override
    _override = store


def reset_object_store() -> None:
    global _override, _cached
    _override = None
    _cached = None


def get_object_store() -> ObjectStore:
    global _cached
    if _override is not None:
        return _override
    if _cached is None:
        _cached = gateway_from_ops()
    return _cached


def gateway_from_ops() -> GatewayStore:
    try:
        row = plain_singleton("s3_client")
    except ConfigError as exc:
        raise ObjectFetchError(exc.status, exc.body) from exc
    endpoint = str(row.get("endpoint") or "").strip()
    bucket = str(row.get("bucket") or "").strip()
    access_key = str(row.get("access_key") or "").strip()
    secret_key = str(row.get("secret_key") or "").strip()
    region = str(row.get("region") or "").strip() or "us-east-1"
    if not endpoint or not bucket or not access_key or not secret_key:
        raise ObjectFetchError(503, {"detail": "s3_client is incomplete"})
    from minio import Minio

    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ObjectFetchError(503, {"detail": "s3_client endpoint is invalid"})
    client = Minio(
        parsed.netloc,
        access_key=access_key,
        secret_key=secret_key,
        secure=parsed.scheme == "https",
        region=region,
    )
    return GatewayStore(client, bucket)


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


def fetch_index_object(asset_path: str) -> IndexObject:
    """Download ``asset_path`` when it is a knowledge object key."""

    key = knowledge_object_key(asset_path)
    path = scratch_path_for_key(key)
    locks = scratch_root() / ".materialize-locks"
    locks.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(path).encode()).hexdigest()
    with (locks / identity).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file() and path.stat().st_size > 0:
            return IndexObject(key, path)
        store = get_object_store()
        if not store.exists(key):
            raise ObjectFetchError(400, {"detail": UNAVAILABLE})
        store.get_to_path(key, path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ObjectFetchError(503, {"detail": "object storage is unavailable"})
    return IndexObject(key, path)


def knowledge_object_key(asset_path: str) -> str:
    text = str(asset_path or "").strip()
    if (
        not text
        or "\x00" in text
        or text.startswith("missing/")
        or text.startswith("/")
        or "\\" in text
        or "://" in text
        or not text.startswith(_KNOWLEDGE_PREFIX)
        or text.endswith("/")
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise ObjectFetchError(400, {"detail": UNAVAILABLE})
    return text


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


def scratch_path_for_key(object_key: str) -> Path:
    relative = PurePosixPath(object_key)
    return scratch_root().joinpath(*relative.parts)
