"""Shared API client fixture."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from viknow_storage.app import create_app
from viknow_storage.blobs import MemoryBlobStore
from viknow_storage.settings import Settings
from viknow_storage_client import StorageClient


class _AppTransport(httpx.BaseTransport):
    def __init__(self, app) -> None:
        self._client = TestClient(app)
        self._client.__enter__()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query.decode()}"
        headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
        response = self._client.request(request.method, path, content=request.read(), headers=headers)
        return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

    def close(self) -> None:
        self._client.__exit__(None, None, None)


@pytest.fixture
def client():
    settings = Settings(
        token="test-token",
        database_url="sqlite://",
        blob_backend="memory",
        s3_endpoint="",
        s3_access_key="",
        s3_secret_key="",
        s3_bucket="viknow",
        s3_region="us-east-1",
        host="127.0.0.1",
        port=8080,
        cors_origins=(),
        grant_secret="grant-test-secret",
        download_base_url="http://storage.test",
        grant_ttl_seconds=86400,
    )
    app = create_app(settings, MemoryBlobStore())
    transport = _AppTransport(app)
    with StorageClient("http://storage", "test-token", transport=transport) as storage:
        storage.transport = transport
        yield storage
