"""Knowledge upload stores the source object and does not start indexing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from viknow_platform.access.rag.service import RagError, upload_object


class KnowledgeUploadTests(unittest.TestCase):
    def test_upload_writes_the_canonical_key_and_does_not_index(self) -> None:
        captured: dict[str, object] = {}

        def fake_upload(library_id, file_name, content, *, asset_id=None, content_type="application/octet-stream"):
            captured.update(
                library_id=library_id,
                file_name=file_name,
                content=content,
                asset_id=asset_id,
                content_type=content_type,
            )

        with patch("viknow_platform.access.knowledge.storage_bridge.upload_bytes", side_effect=fake_upload):
            with patch("viknow_platform.access.rag.service.HYPERAG.create_index") as create:
                uploaded = upload_object("library-1", "动作演示.mp4", b"video", asset_id="asset-1", content_type="video/mp4")
        create.assert_not_called()
        key = "knowledge/assets/asset-1/upload/asset-1.mp4"
        self.assertEqual(uploaded["asset_path"], key)
        self.assertEqual(uploaded["file_name"], "动作演示.mp4")
        self.assertEqual(uploaded["size_bytes"], 5)
        self.assertEqual(captured["library_id"], "library-1")
        self.assertEqual(captured["asset_id"], "asset-1")
        self.assertEqual(captured["content"], b"video")

    def test_upload_rejects_a_missing_extension(self) -> None:
        with patch("viknow_platform.access.knowledge.storage_bridge.upload_bytes") as upload:
            with self.assertRaises(RagError) as caught:
                upload_object("library-1", "notes", b"hello")
        self.assertEqual(caught.exception.status, 422)
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
