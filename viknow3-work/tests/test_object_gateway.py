"""fetch_index_asset materializes viknow-storage bytes onto scratch."""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from viknow3.service.hyperrag.object_gateway import ObjectFetchError, fetch_index_asset


class FetchIndexAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        import viknow3.service.hyperrag.object_gateway as og

        og.scratch_root().mkdir(parents=True, exist_ok=True)

    def test_downloads_from_storage_client(self) -> None:
        library_id = f"lib-{uuid.uuid4().hex[:8]}"
        asset_id = "clip-1"
        payload = b"video-bytes"

        class FakeClient:
            def download(self, lib: str, asset: str) -> bytes:
                self.lib = lib
                self.asset = asset
                return payload

        with mock.patch(
            "viknow3.service.storage.gateway.get_storage_client",
            return_value=FakeClient(),
        ):
            fetched = fetch_index_asset(library_id, asset_id)

        self.assertEqual(fetched.key, f"storage://{library_id}/{asset_id}")
        self.assertTrue(fetched.path.is_file())
        self.assertEqual(fetched.path.read_bytes(), payload)

        with mock.patch(
            "viknow3.service.storage.gateway.get_storage_client",
            return_value=FakeClient(),
        ):
            again = fetch_index_asset(library_id, asset_id)
        self.assertEqual(again.path, fetched.path)

    def test_missing_ids_rejected(self) -> None:
        with self.assertRaises(ObjectFetchError) as caught:
            fetch_index_asset("", "a")
        self.assertEqual(caught.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
