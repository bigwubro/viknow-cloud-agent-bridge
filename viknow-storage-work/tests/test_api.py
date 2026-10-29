"""Library, asset, and byte calls against the in-memory blob store."""

from __future__ import annotations

import httpx
import pytest

from viknow_storage_client import StorageClient, StorageClientError


def test_library_and_asset_round_trip(client: StorageClient):
    created = client.create_library("demo-lib", "演示库")
    assert created["library_id"] == "demo-lib"
    assert created["title"] == "演示库"
    assert client.list_libraries()[0]["library_id"] == "demo-lib"
    assert client.get_library("demo-lib")["title"] == "演示库"

    asset = client.upload_asset("demo-lib", "notes.pdf", b"%PDF-1.4 notes", content_type="application/pdf", asset_id="notes-1")
    assert asset["asset_id"] == "notes-1"
    assert asset["modality"] == "document"
    assert "object_key" not in asset
    assert "asset_path" not in asset
    assert client.asset_exists("demo-lib", "notes-1")
    assert client.get_asset("demo-lib", "notes-1")["filename"] == "notes.pdf"
    assert client.list_assets("demo-lib")[0]["asset_id"] == "notes-1"
    assert client.download("demo-lib", "notes-1") == b"%PDF-1.4 notes"

    status, body = client.preview("demo-lib", "notes-1", range_header="bytes=0-3")
    assert status == 206
    assert body == b"%PDF"

    client.delete_asset("demo-lib", "notes-1")
    assert client.asset_exists("demo-lib", "notes-1") is False
    with pytest.raises(StorageClientError) as caught:
        client.download("demo-lib", "notes-1")
    assert caught.value.status == 404


def test_video_modality_and_library_soft_delete(client: StorageClient):
    client.create_library("media")
    asset = client.upload_asset("media", "clip.mp4", b"video-bytes", content_type="video/mp4")
    assert asset["modality"] == "video"
    client.delete_library("media")
    with pytest.raises(StorageClientError) as caught:
        client.get_library("media")
    assert caught.value.status == 404
    restored = client.create_library("media", "回来了")
    assert restored["title"] == "回来了"


def test_rejects_missing_token_and_bad_names(client: StorageClient):
    bare = httpx.Client(base_url="http://storage", transport=client.transport)
    response = bare.post("/libraries", json={"library_id": "x"})
    assert response.status_code == 401
    with pytest.raises(StorageClientError) as caught:
        client.create_library("../etc")
    assert caught.value.status == 422
    client.create_library("ok")
    with pytest.raises(StorageClientError) as caught:
        client.upload_asset("ok", "noext", b"abc")
    assert caught.value.status == 422
    with pytest.raises(StorageClientError) as caught:
        client.upload_asset("missing", "a.txt", b"abc")
    assert caught.value.status == 404
