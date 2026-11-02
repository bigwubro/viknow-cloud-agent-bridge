"""Download URL grants."""

from __future__ import annotations

import httpx

from viknow_storage_client import StorageClient


def test_mint_download_url_and_fetch_without_bearer(client: StorageClient):
    client.create_library("grant-lib")
    client.upload_asset("grant-lib", "clip.mp4", b"video-grant-bytes", content_type="video/mp4", asset_id="v1")
    issued = client.mint_download_url("grant-lib", "v1")
    assert issued["library_id"] == "grant-lib"
    assert issued["asset_id"] == "v1"
    assert issued["ttl_seconds"] == 86400
    assert "grant=" in issued["url"]
    assert "expires_at" in issued

    bare = httpx.Client(transport=client.transport, base_url="http://storage")
    from urllib.parse import urlparse

    path = urlparse(issued["url"]).path
    query = urlparse(issued["url"]).query
    response = bare.get(f"{path}?{query}")
    assert response.status_code == 200
    assert response.content == b"video-grant-bytes"
