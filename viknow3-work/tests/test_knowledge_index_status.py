"""Catalog index status API and job completion writeback."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()

from viknow3.access.knowledge.index_status import (
    get_asset_index_status,
    list_library_assets_index_status,
    persist_index_job_completion,
    sync_index_job_from_api,
)


class IndexStatusTests(unittest.TestCase):
    def test_persist_index_job_completion_writes_catalog(self) -> None:
        with mock.patch("viknow3.access.knowledge.index_status.store.set_current_index_version_id") as setter:
            persist_index_job_completion(
                {
                    "job_id": "job-9",
                    "library_id": "lib-1",
                    "asset_id": "a-1",
                    "status": "done",
                }
            )
            setter.assert_called_once_with("lib-1", "a-1", "job-9")

    def test_sync_index_job_from_api_upserts_row(self) -> None:
        with mock.patch("viknow3.access.knowledge.index_status.store.upsert_index_job_snapshot") as upsert:
            with mock.patch("viknow3.access.knowledge.index_status.store.set_current_index_version_id") as setter:
                sync_index_job_from_api(
                    {
                        "job_id": "job-1",
                        "library_id": "lib-1",
                        "asset_id": "a-1",
                        "status": "done",
                    }
                )
        upsert.assert_called_once()
        setter.assert_called_once_with("lib-1", "a-1", "job-1")

    def test_persist_skips_non_terminal(self) -> None:
        with mock.patch("viknow3.access.knowledge.index_status.store.set_current_index_version_id") as setter:
            persist_index_job_completion({"job_id": "j", "library_id": "l", "asset_id": "a", "status": "running"})
            setter.assert_not_called()

    def test_indexed_from_catalog_column(self) -> None:
        asset = mock.Mock(current_index_version_id="ver-1")
        with mock.patch("viknow3.access.knowledge.index_status.store.get_asset", return_value=asset):
            payload = get_asset_index_status("lib-1", "a-1")
        self.assertEqual(payload["status"], "indexed")
        self.assertEqual(payload["current_index_version_id"], "ver-1")

    def test_hyperrag_completed_backfills_catalog(self) -> None:
        asset = mock.Mock(current_index_version_id=None)
        refreshed = mock.Mock(current_index_version_id="hr-row-1")
        with mock.patch("viknow3.access.knowledge.index_status.store.get_asset", side_effect=[asset, refreshed]):
            with mock.patch("viknow3.access.knowledge.index_status.store.get_latest_index_job", return_value=None):
                with mock.patch(
                    "viknow3.access.knowledge.index_status.store.get_latest_hyperrag_asset_status",
                    return_value={
                        "workspace": "lib-1",
                        "id": "hr-row-1",
                        "asset_id": "a-1",
                        "status": "completed",
                        "error_message": None,
                    },
                ):
                    with mock.patch("viknow3.access.knowledge.index_status.store.upsert_index_job_snapshot"):
                        with mock.patch(
                            "viknow3.access.knowledge.index_status.store.set_current_index_version_id"
                        ) as setter:
                            payload = get_asset_index_status("lib-1", "a-1")
                            setter.assert_called_once_with("lib-1", "a-1", "hr-row-1")
                            self.assertEqual(payload["status"], "indexed")
                            self.assertEqual(payload["current_index_version_id"], "hr-row-1")

    def test_list_library_assets_index_status_batch(self) -> None:
        asset_a = mock.Mock(asset_id="a-1")
        asset_b = mock.Mock(asset_id="a-2")
        with mock.patch("viknow3.access.knowledge.index_status.store.get_library", return_value=mock.Mock()):
            with mock.patch(
                "viknow3.access.knowledge.index_status.store.list_assets",
                return_value=[asset_a, asset_b],
            ):
                with mock.patch(
                    "viknow3.access.knowledge.index_status.get_asset_index_status",
                    side_effect=[
                        {"asset_id": "a-1", "status": "indexed"},
                        {"asset_id": "a-2", "status": "not_indexed"},
                    ],
                ) as getter:
                    rows = list_library_assets_index_status("lib-1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(getter.call_count, 2)


if __name__ == "__main__":
    unittest.main()
