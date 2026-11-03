"""Index artifacts under /libraries/.../assets/{asset_id}/artifacts."""

from __future__ import annotations

import pytest

from viknow_storage_client import StorageClient, StorageClientError


def test_artifact_round_trip(client: StorageClient):
    client.create_library("lib-art")
    client.upload_asset("lib-art", "x.txt", b"hello", asset_id="f1")

    meta = client.upload_artifact(
        "lib-art",
        "f1",
        artifact_id="parsed-md",
        filename="parsed.md",
        content=b"# title\n",
        content_type="text/markdown",
    )
    assert meta["artifact_id"] == "parsed-md"
    assert "object_key" not in meta

    listed = client.list_artifacts("lib-art", "f1")
    assert listed[0]["artifact_id"] == "parsed-md"

    assert client.download_artifact("lib-art", "f1", "parsed-md") == b"# title\n"
    assert client.get_artifact("lib-art", "f1", "parsed-md")["content_type"] == "text/markdown"

    client.delete_artifact("lib-art", "f1", "parsed-md")
    with pytest.raises(StorageClientError) as caught:
        client.get_artifact("lib-art", "f1", "parsed-md")
    assert caught.value.status == 404


def test_rejects_duplicate_artifact(client: StorageClient):
    client.create_library("dup")
    client.upload_asset("dup", "a.pdf", b"x", asset_id="a")
    client.upload_artifact(
        "dup",
        "a",
        artifact_id="manifest",
        filename="manifest.json",
        content=b"{}",
    )
    with pytest.raises(StorageClientError) as caught:
        client.upload_artifact(
            "dup",
            "a",
            artifact_id="manifest",
            filename="manifest.json",
            content=b"[]",
        )
    assert caught.value.status == 409


def test_artifact_requires_asset(client: StorageClient):
    client.create_library("only-lib")
    with pytest.raises(StorageClientError) as caught:
        client.upload_artifact(
            "only-lib",
            "missing",
            artifact_id="x",
            filename="x.txt",
            content=b"x",
        )
    assert caught.value.status == 404
