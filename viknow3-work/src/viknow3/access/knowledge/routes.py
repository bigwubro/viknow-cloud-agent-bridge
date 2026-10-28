"""Knowledge HTTP routes (index, status, search only).

Library/asset blobs live in viknow-storage; Playground calls storage directly.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from viknow3.access.auth import AppKey, require_app
from viknow3.access.knowledge import service

router = APIRouter(prefix="/api/v1/knowledge", dependencies=[Depends(require_app)])


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    if not isinstance(parsed, dict):
        raise service.KnowledgeError(422, {"detail": "expected JSON object"})
    return parsed


@router.get("/libraries/{library_id}/assets/index-status-batch")
def library_assets_index_status_batch(
    library_id: str,
    limit: int | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    del app_key
    return service.list_library_assets_index_status(library_id, limit)


@router.get("/libraries/{library_id}/assets/{asset_id}/index-status")
def asset_index_status(
    library_id: str,
    asset_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    del app_key
    return service.get_asset_index_status(library_id, asset_id)


@router.get("/libraries/{library_id}/assets/{asset_id}/index-jobs/latest")
def latest_asset_index_job(
    library_id: str,
    asset_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    del app_key
    return service.get_latest_asset_index_job(library_id, asset_id)


@router.post("/index-jobs")
async def create_index_job(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    del app_key
    return service.create_index_job(await _json(request))


@router.get("/index-jobs/{job_id}")
def get_index_job(job_id: str) -> dict[str, Any]:
    return service.get_index_job(job_id)


@router.get("/index-jobs/{job_id}/tasks")
def list_index_job_tasks(job_id: str) -> list[dict[str, Any]]:
    return service.get_index_job_tasks(job_id)


@router.delete("/indexes")
async def delete_index(request: Request) -> dict[str, Any]:
    body = await _json(request)
    return service.delete_index(str(body.get("library_id") or ""), str(body.get("asset_id") or ""))


@router.post("/search")
async def search(request: Request) -> dict[str, Any]:
    return service.search(await _json(request))


online_router = APIRouter(dependencies=[Depends(require_app)])


@online_router.get("/api/v1/online/libraries/{library_id}/assets/{asset_id}/index")
def asset_index(library_id: str, asset_id: str) -> dict[str, Any]:
    return service.asset_index(library_id, asset_id)
