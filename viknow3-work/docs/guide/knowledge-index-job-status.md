# Knowledge index job status (polling)

`GET /api/v1/knowledge/index-jobs/{job_id}` returns the HyperRAG indexing job stored in Redis (`hyperrag:job:*` / `hyperrag:task:*`). Requires the same `X-App-Key` auth as other knowledge routes.

## Response example

```json
{
  "job_id": "8f3c2b1a-4d5e-6f70-8192-a3b4c5d6e7f8",
  "library_id": "my-library",
  "asset_id": "asset-42",
  "status": "running",
  "error_message": null,
  "metadata": null,
  "created_at": "2026-10-22T01:15:30.123456+00:00",
  "completed_at": null,
  "progress": {
    "overall": 0.25,
    "current_stage": "embed",
    "current_stage_index": 1,
    "stage_count": 4,
    "completed_stage_count": 1,
    "running_duration_seconds": 12.5,
    "stages": [
      {
        "name": "ingest",
        "status": "completed",
        "task_id": "task-0",
        "started_at": "2026-10-22T01:15:30.123456+00:00",
        "completed_at": "2026-10-22T01:15:40.123456+00:00",
        "error_message": null
      },
      {
        "name": "embed",
        "status": "running",
        "task_id": "task-1",
        "started_at": "2026-10-22T01:15:40.123456+00:00",
        "completed_at": null,
        "error_message": null
      }
    ]
  }
}
```

When `status` is `done`, `metadata` is populated with [`IndexJobMetadata`](http://36.103.198.236:5208/start/sdk/types.html#index-job-metadata). `progress.overall` is a fraction in `0.0`–`1.0` derived from completed pipeline stages (no fabricated in-stage percentages).


## Task list endpoint

`GET /api/v1/knowledge/index-jobs/{job_id}/tasks` returns the same pipeline tasks as `progress.stages` on the job poll, as a top-level JSON array (ordered by `stage_index`).

- Blank `job_id` → HTTP 422, `detail`: `job_id is required`
- Unknown job → HTTP 404, `detail`: `index job not found`
- Job exists but no task metadata yet → HTTP 200, `[]`

```json
[
  {
    "task_id": "task-0",
    "name": "ingest",
    "stage": "ingest",
    "stage_index": 0,
    "status": "completed",
    "progress": null,
    "started_at": "2026-10-22T01:15:30.123456+00:00",
    "completed_at": "2026-10-22T01:15:40.123456+00:00",
    "error_message": null
  }
]
```

`progress` on each task is reserved for future in-stage percentages; Redis does not store them today (always `null`).
