"""Knowledge upload stores the source object and does not start indexing."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from viknow3.access.knowledge.service import KnowledgeError, upload_object
from viknow3.application.hyperrag.object_gateway import install_object_store, reset_object_store


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def get_to_path(self, key: str, path: Path) -> None:
        path.write_bytes(self.objects[key])

    def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
        del content_type
        self.objects[key] = payload


class KnowledgeUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _Store()
        install_object_store(self.store)

    def tearDown(self) -> None:
        reset_object_store()

    def test_upload_writes_the_canonical_key_and_does_not_index(self) -> None:
        with patch("viknow3.access.knowledge.service.HYPERAG.create_index") as create:
            uploaded = upload_object("library-1", "动作演示.mp4", b"video", asset_id="asset-1", content_type="video/mp4")
        create.assert_not_called()
        key = "knowledge/assets/asset-1/upload/asset-1.mp4"
        self.assertEqual(uploaded["asset_path"], key)
        self.assertEqual(uploaded["file_name"], "动作演示.mp4")
        self.assertEqual(uploaded["size_bytes"], 5)
        self.assertEqual(self.store.objects[key], b"video")

    def test_upload_rejects_a_missing_extension(self) -> None:
        with self.assertRaises(KnowledgeError) as caught:
            upload_object("library-1", "notes", b"hello")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.store.objects, {})
