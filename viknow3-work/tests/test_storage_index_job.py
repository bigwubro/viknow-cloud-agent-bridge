"""Index jobs pull bytes from viknow-storage when asset_path is omitted."""

from __future__ import annotations

import unittest
import uuid
from unittest import mock

from viknow3.access.knowledge import store
from viknow3.access.knowledge.service import KnowledgeError, create_index_job
from viknow3.service.hyperrag.object_gateway import install_object_store, reset_object_store
from viknow3.service.storage.client import StorageClientError


class _MemoryStore:
    def exists(self, key: str) -> bool:
        return False

    def get_to_path(self, key: str, path) -> None:
        raise AssertionError("legacy store should not be used")

    def put_bytes(self, key: str, payload: bytes, *, content_type: str = "application/octet-stream") -> None:
        del key, payload, content_type


class StorageIndexJobTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_object_store()
        install_object_store(_MemoryStore())

    def tearDown(self) -> None:
        reset_object_store()

    def test_create_index_job_without_asset_path_uses_storage(self) -> None:
        library_id = f"lib-a-{uuid.uuid4().hex[:8]}"
        asset_id = "file-1"
        store.create_library(library_id, "A")
        captured: dict = {}

        class FakeClient:
            def asset_exists(self, library_id: str, asset_id: str) -> bool:
                self.library_id = library_id
                self.asset_id = asset_id
                return True

            def get_asset(self, library_id: str, asset_id: str) -> dict:
                return {
                    "library_id": library_id,
                    "asset_id": asset_id,
                    "title": "demo.pdf",
                    "modality": "document",
                }

            def download(self, library_id: str, asset_id: str) -> bytes:
                return b"%PDF-demo"

        def fake_create_index(library_id, asset_id, path, title, compile_mode=None, object_key=None):
            captured.update(
                {
                    "library_id": library_id,
                    "asset_id": asset_id,
                    "path": path,
                    "object_key": object_key,
                }
            )
            return {"job_id": "job-1", "status": "pending"}

        client = FakeClient()
        with (
            mock.patch("viknow3.access.knowledge.service.storage_configured", return_value=True),
            mock.patch("viknow3.access.knowledge.service.get_storage_client", return_value=client),
            mock.patch("viknow3.service.storage.gateway.get_storage_client", return_value=client),
            mock.patch("viknow3.access.knowledge.service.HYPERAG.create_index", side_effect=fake_create_index),
        ):
            job = create_index_job({"library_id": library_id, "asset_id": asset_id})

        self.assertEqual(job["job_id"], "job-1")
        self.assertEqual(captured["object_key"], f"storage://{library_id}/{asset_id}")
        self.assertTrue(captured["path"])
        row = store.get_asset(library_id, asset_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.asset_path, f"storage://{library_id}/{asset_id}")

    def test_asset_path_in_body_rejected(self) -> None:
        with mock.patch("viknow3.access.knowledge.service.storage_configured", return_value=True):
            with self.assertRaises(KnowledgeError) as caught:
                create_index_job({"library_id": "lib-x", "asset_id": "a", "asset_path": "knowledge/assets/a"})
        self.assertEqual(caught.exception.status, 422)

    def test_storage_missing_asset_is_404(self) -> None:
        class FakeClient:
            def asset_exists(self, library_id: str, asset_id: str) -> bool:
                return False

        with (
            mock.patch("viknow3.access.knowledge.service.storage_configured", return_value=True),
            mock.patch("viknow3.access.knowledge.service.get_storage_client", return_value=FakeClient()),
        ):
            with self.assertRaises(KnowledgeError) as caught:
                create_index_job({"library_id": f"lib-miss-{uuid.uuid4().hex[:8]}", "asset_id": "missing"})
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
