"""viknow-storage access for the knowledge layer."""

from __future__ import annotations

import os
from functools import lru_cache

from viknow3.service.storage.client import StorageClient


def storage_configured() -> bool:
    return bool(os.environ.get("VIKNOW_STORAGE_URL", "").strip() and os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip())


@lru_cache(maxsize=1)
def get_storage_client() -> StorageClient:
    base = os.environ.get("VIKNOW_STORAGE_URL", "").strip().rstrip("/")
    token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
    if not base or not token:
        raise RuntimeError("viknow-storage is not configured")
    return StorageClient(base, token)
