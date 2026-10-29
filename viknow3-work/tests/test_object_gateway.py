"""Index jobs materialize assets via viknow-storage (no direct MinIO in viknow3)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from viknow3.access.rag.service import RagError, _catalog_asset, create_index_job
from viknow3.application.hyperrag.client import HyperragClient
from viknow3.service.hyperrag.object_gateway import IndexObject, ObjectFetchError


class ObjectGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("VIKNOW_SCRATCH_DIR")
        os.environ["VIKNOW_SCRATCH_DIR"] = self._scratch.name
        os.environ.setdefault("VIKNOW_ACCESS_DSN", "postgresql://u:p@127.0.0.1:1/a")
        os.environ.setdefault("VIKNOW_AGENT_DSN", "postgresql://u:p@127.0.0.1:1/b")
        os.environ.setdefault("VIKNOW_HYPERRAG_DSN", "postgresql://u:p@127.0.0.1:1/c")

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("VIKNOW_SCRATCH_DIR", None)
        else:
            os.environ["VIKNOW_SCRATCH_DIR"] = self._previous
        self._scratch.cleanup()

    def test_index_job_hands_the_local_file_to_hyperrag_and_keeps_the_key(self) -> None:
        key = "knowledge/assets/asset-1/upload/asset-1.mp4"
        local = Path(self._scratch.name) / "asset-1.mp4"
        local.write_bytes(b"asset")
        fetched = IndexObject(key=key, path=local)
        captured: dict[str, object] = {}

        def fake_create(library_id, asset_id, asset_path, title, compile_mode=None, object_key=None):
            captured.update(
                library_id=library_id,
                asset_id=asset_id,
                asset_path=asset_path,
                title=title,
                compile_mode=compile_mode,
                object_key=object_key,
            )
            return {
                "job_id": "job-1",
                "library_id": library_id,
                "asset_id": asset_id,
                "status": "pending",
                "error_message": None,
                "metadata": None,
            }

        class _Asset:
            asset_path = key
            title = "动作演示.mp4"

        with patch("viknow3.pg.connect", return_value=MagicMock()):
            with patch("viknow3.access.rag.service.fetch_index_asset", return_value=fetched):
                with patch("viknow3.access.rag.service._catalog_asset", return_value=_Asset()):
                    with patch("viknow3.access.rag.service.HYPERAG.create_index", side_effect=fake_create):
                        job = create_index_job(
                        {
                            "library_id": "library-1",
                            "asset_id": "asset-1",
                            "asset_path": key,
                            "compile_mode": "fast_ingest",
                            "params": {"title": "动作演示.mp4"},
                        },
                    )
        self.assertEqual(job["job_id"], "job-1")
        self.assertEqual(captured["object_key"], key)
        self.assertEqual(captured["title"], "动作演示.mp4")
        self.assertEqual(Path(str(captured["asset_path"])).read_bytes(), b"asset")

    def test_index_job_rejects_a_missing_key_before_hyperrag(self) -> None:
        key = "knowledge/assets/asset-1/upload/asset-1.mp4"

        class _Asset:
            asset_path = key
            title = "动作演示.mp4"

        with patch(
            "viknow3.access.rag.service.fetch_index_asset",
            side_effect=ObjectFetchError(404, {"detail": "missing"}),
        ):
            with patch("viknow3.access.rag.service._catalog_asset", return_value=_Asset()):
                with patch("viknow3.access.rag.service.HYPERAG.create_index") as create:
                    with self.assertRaises(RagError) as caught:
                        create_index_job(
                            {
                                "library_id": "library-1",
                                "asset_id": "asset-1",
                                "asset_path": key,
                            },
                        )
        self.assertEqual(caught.exception.status, 404)
        create.assert_not_called()

    def test_index_job_requires_the_library_and_asset_first(self) -> None:
        class MissingLibrary:
            def get_library(self, library_id: str) -> None:
                return None

            def get_asset(self, library_id: str, asset_id: str) -> None:
                raise AssertionError("asset lookup before the library exists")

        with self.assertRaises(RagError) as missing_library:
            _catalog_asset("library-1", "asset-1", store=MissingLibrary())
        self.assertEqual(missing_library.exception.status, 404)

        class MissingAsset:
            def get_library(self, library_id: str) -> object:
                return object()

            def get_asset(self, library_id: str, asset_id: str) -> None:
                return None

        with self.assertRaises(RagError) as missing_asset:
            _catalog_asset("library-1", "asset-1", store=MissingAsset())
        self.assertEqual(missing_asset.exception.body["detail"], "asset not found")

        key = "knowledge/assets/asset-1/upload/asset-1.mp4"

        class OtherPath:
            asset_path = "knowledge/assets/other/upload/other.mp4"
            title = "其他"

        with patch("viknow3.access.rag.service._catalog_asset", return_value=OtherPath()):
            with patch("viknow3.access.rag.service.HYPERAG.create_index") as create:
                with self.assertRaises(RagError) as mismatch:
                    create_index_job(
                        {"library_id": "library-1", "asset_id": "asset-1", "asset_path": key},
                    )
        self.assertEqual(mismatch.exception.status, 422)
        create.assert_not_called()

    def test_client_records_the_object_key_on_the_job(self) -> None:
        captured: dict[str, object] = {}

        class _Accepted:
            job_id = "job-1"
            library_id = "library-1"
            asset_id = "asset-1"

        class _Adapter:
            def submit_index(self, **kwargs):
                captured.update(kwargs)
                return _Accepted()

        client = HyperragClient.__new__(HyperragClient)
        client._adapter = _Adapter()
        job = client.create_index(
            "library-1",
            "asset-1",
            "/tmp/viknow-scratch/knowledge/assets/asset-1/upload/asset-1.mp4",
            "动作演示.mp4",
            compile_mode="fast_ingest",
            object_key="knowledge/assets/asset-1/upload/asset-1.mp4",
        )
        self.assertEqual(job["status"], "pending")
        self.assertEqual(captured["asset_path"], "/tmp/viknow-scratch/knowledge/assets/asset-1/upload/asset-1.mp4")


if __name__ == "__main__":
    unittest.main()
