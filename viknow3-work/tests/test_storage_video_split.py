"""Actio split media hook for viknow-storage assets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from viknow3.service.hyperrag import storage_video_split as svs


class StorageVideoSplitTests(unittest.TestCase):
    def test_storage_split_media_from_scratch_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "storage" / "lib-a" / "asset-9" / "original.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            with (
                patch.object(svs, "scratch_root", return_value=root),
                patch.object(svs, "storage_configured", return_value=True),
                patch.object(svs, "get_storage_client") as client_factory,
            ):
                client_factory.return_value.asset_exists.return_value = True
                media = svs.storage_split_media(str(video))
            self.assertIsNotNone(media)
            assert media is not None
            self.assertEqual(media["type"], "viknow_storage")
            self.assertEqual(media["library_id"], "lib-a")
            self.assertEqual(media["asset_id"], "asset-9")

    def test_storage_split_media_rejects_non_storage_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"x")
            with patch.object(svs, "storage_configured", return_value=True):
                self.assertIsNone(svs.storage_split_media(str(path)))


if __name__ == "__main__":
    unittest.main()
