"""Index job polling payload from HyperRAG Redis progress."""

from __future__ import annotations

import unittest
from unittest.mock import patch


from viknow3.access.rag.service import RagError, get_index_job_tasks
from viknow3.application.hyperrag.job_progress import (
    JobProgress,
    JobStageProgress,
    index_job_from_progress,
    index_job_tasks_from_progress,
    job_progress_to_api,
)


class HyperragJobProgressApiTests(unittest.TestCase):
    def test_job_progress_to_api_running(self) -> None:
        progress = JobProgress(
            job_id="job-1",
            job_name="index",
            workspace="lib-1",
            asset_id="asset-1",
            status="running",
            stages=[
                JobStageProgress(name="ingest", status="completed", task_id="t0", started_at=1.0, completed_at=2.0),
                JobStageProgress(name="embed", status="running", task_id="t1", started_at=3.0),
            ],
            current_stage_index=1,
            overall_progress=0.5,
            created_at=1.0,
            running_duration_seconds=10.0,
        )
        payload = job_progress_to_api(progress)
        self.assertEqual(payload["overall"], 0.5)
        self.assertEqual(payload["current_stage"], "embed")
        self.assertEqual(len(payload["stages"]), 2)
        self.assertEqual(payload["stages"][0]["started_at"], "1970-01-01T00:00:01+00:00")

    def test_index_job_from_progress_failed_uses_stage_error(self) -> None:
        progress = JobProgress(
            job_id="job-2",
            job_name="index",
            workspace="lib-1",
            asset_id="asset-1",
            status="failed",
            stages=[
                JobStageProgress(
                    name="ingest",
                    status="failed",
                    task_id="t0",
                    error_message="disk full",
                ),
            ],
            current_stage_index=0,
            overall_progress=0.0,
            created_at=100.0,
            completed_at=200.0,
        )
        body = index_job_from_progress(progress, status="failed", metadata=None)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error_message"], "disk full")
        self.assertIn("progress", body)
        self.assertEqual(body["created_at"], "1970-01-01T00:01:40+00:00")



    def test_index_job_tasks_from_progress(self) -> None:
        progress = JobProgress(
            job_id="job-3",
            job_name="index",
            workspace="lib-1",
            asset_id="asset-1",
            status="running",
            stages=[
                JobStageProgress(name="ingest", status="completed", task_id="t0", started_at=1.0, completed_at=2.0),
                JobStageProgress(name="embed", status="running", task_id="t1", started_at=3.0),
            ],
            current_stage_index=1,
            overall_progress=0.5,
            created_at=1.0,
        )
        tasks = index_job_tasks_from_progress(progress)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["task_id"], "t0")
        self.assertEqual(tasks[0]["name"], "ingest")
        self.assertEqual(tasks[0]["stage"], "ingest")
        self.assertEqual(tasks[0]["stage_index"], 0)
        self.assertIsNone(tasks[0]["progress"])
        self.assertEqual(tasks[1]["status"], "running")

    def test_index_job_tasks_empty_stages(self) -> None:
        progress = JobProgress(
            job_id="job-4",
            job_name="index",
            workspace="lib-1",
            asset_id="asset-1",
            status="pending",
            stages=[],
            current_stage_index=0,
            overall_progress=0.0,
        )
        self.assertEqual(index_job_tasks_from_progress(progress), [])

    def test_get_index_job_tasks_service_422(self) -> None:
        with self.assertRaises(RagError) as ctx:
            get_index_job_tasks("   ")
        self.assertEqual(ctx.exception.status, 422)

    def test_get_index_job_tasks_service_delegates(self) -> None:
        import viknow3.access.rag.service as rag_service

        with patch.object(rag_service.HYPERAG, "get_index_job_tasks", return_value=[]) as mocked:
            self.assertEqual(get_index_job_tasks("job-1"), [])
        mocked.assert_called_once_with("job-1")


if __name__ == "__main__":
    unittest.main()
