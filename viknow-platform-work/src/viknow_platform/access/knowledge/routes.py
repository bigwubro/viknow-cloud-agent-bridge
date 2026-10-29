"""Knowledge HTTP routes (/api/v1/knowledge/libraries …)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from viknow_platform.access.auth import AppKey, require_app
from viknow_platform.access.knowledge import service

router = APIRouter(prefix="/api/v1/knowledge", dependencies=[Depends(require_app)])


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    if not isinstance(parsed, dict):
        raise service.KnowledgeError(422, {"detail": "expected JSON object"})
    return parsed


@router.post("/libraries", status_code=201)
async def create_library(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    body = await _json(request)
    allowed = {"library_id", "title"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise service.KnowledgeError(422, {"detail": [{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown]})
    return service.create_library(body)


@router.get("/libraries")
def list_libraries(
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_libraries(limit, after)


@router.get("/libraries/{library_id}")
def get_library(library_id: str, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.get_library(library_id)


@router.post("/libraries/{library_id}/assets", status_code=201)
async def create_asset(
    library_id: str,
    request: Request,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    body = await _json(request)
    allowed = {"asset_id", "asset_path", "title", "modality"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise service.KnowledgeError(422, {"detail": [{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown]})
    return service.create_asset(library_id, body)


@router.get("/libraries/{library_id}/assets")
def list_assets(
    library_id: str,
    limit: int | None = None,
    after: str | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, Any]]:
    return service.list_assets(library_id, limit, after)


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
