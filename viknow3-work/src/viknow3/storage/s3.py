"""Deprecated: viknow3 must not connect to MinIO/S3 directly. Use viknow-storage (VIKNOW_STORAGE_URL)."""

from __future__ import annotations

_MSG = "direct S3/MinIO is disabled; configure VIKNOW_STORAGE_URL + VIKNOW_STORAGE_TOKEN"


class StorageError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def _disabled(*_args: object, **_kwargs: object) -> None:
    raise StorageError(503, _MSG)


head_object = _disabled
put_object_stream = _disabled
presign_put = _disabled
presign_get = _disabled
