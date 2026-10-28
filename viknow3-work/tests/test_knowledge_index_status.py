"""Index status API (storage + jobs / HyperRAG, no PG catalog)."""

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
from viknow3.access.knowledge import store
from viknow3.clock import now


class IndexStatusTests(unittest.TestCase):
    def test_persist_index_job_completion_is_noop(self) -> None:
        persist_index_job_completion(
            {"job_id": "job-9", "library_id": "lib-1", "asset_id": "a-1", "status": "done"}
        )

    def test_sync_index_job_from_api_upserts_row(self) -> None:
        with mock.patch("viknow3.access.knowledge.index_status.store.upsert_index_job_snapshot") as upsert:
            sync_index_job_from_api(
                {
                    "job_id": "job-1",
                    "library_id": "lib-1",
                    "asset_id": "a-1",
                    "status": "done",
                }
            )
        upsert.assert_called_once()

    def test_indexed_from_persisted_job(self) -> None:
        job = store.IndexJobRow(
            "job-done",
            "lib-1",
            "a-1",
            "done",
            None,
            now(),
            None,
        )
        with mock.patch("viknow3.access.knowledge.index_status._assert_storage_asset"):
            with mock.patch(
                "viknow3.access.knowledge.index_status.store.get_latest_index_job",
                return_value=job,
            ):
                with mock.patch(
                    "viknow3.access.knowledge.index_status.store.get_latest_hyperrag_asset_status",
                    return_value=None,
                ):
                    with mock.patch("viknow3.access.knowledge.index_status._redis_job_snapshot", return_value=None):
                        payload = get_asset_index_status("lib-1", "a-1")
        self.assertEqual(payload["status"], "indexed")
        self.assertEqual(payload["current_index_version_id"], "job-done")

    def test_hyperrag_completed_returns_indexed(self) -> None:
        with mock.patch("viknow3.access.knowledge.index_status._assert_storage_asset"):
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
                        payload = get_asset_index_status("lib-1", "a-1")
        self.assertEqual(payload["status"], "indexed")
        self.assertEqual(payload["current_index_version_id"], "hr-row-1")

    def test_list_library_assets_index_status_batch(self) -> None:
        class FakeClient:
            def get_library(self, library_id: str) -> dict:
                return {"library_id": library_id}

            def list_assets(self, library_id: str, *, limit: int | None = None):
                return [{"asset_id": "a-1"}, {"asset_id": "a-2"}]

        with mock.patch("viknow3.access.knowledge.index_status.storage_configured", return_value=True):
            with mock.patch(
                "viknow3.access.knowledge.index_status.get_storage_client",
                return_value=FakeClient(),
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
