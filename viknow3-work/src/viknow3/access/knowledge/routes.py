"""Knowledge HTTP routes (/api/v1/knowledge/libraries …)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

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


@router.post("/libraries/{library_id}/uploads")
async def presign_library_upload(
    library_id: str,
    request: Request,
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    """Presigned PUT URL for async upload to the object gateway (before POST …/assets)."""

    body = await _json(request)
    allowed = {"file_name", "content_type", "asset_id"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise service.KnowledgeError(
            422,
            {"detail": [{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown]},
        )
    file_name = str(body.get("file_name") or "").strip()
    if not file_name:
        raise service.KnowledgeError(422, {"detail": [{"loc": ["body", "file_name"], "msg": "field required", "type": "missing"}]})
    service.get_library(library_id)
    content_type = str(body.get("content_type") or "application/octet-stream")
    asset_id = body.get("asset_id")
    asset_id_text = str(asset_id).strip() if asset_id is not None else None
    return service.presign_upload(library_id, file_name, asset_id_text, content_type)


@router.post("/libraries/{library_id}/uploads/direct")
async def direct_library_upload(
    library_id: str,
    file: UploadFile = File(...),
    app_key: AppKey = Depends(require_app),
) -> dict[str, Any]:
    """Multipart upload via Playground API (server writes to S3; no browser presigned PUT)."""

    del app_key
    return service.upload_library_file(
        library_id,
        file.filename or "",
        file.file,
        file.content_type or "application/octet-stream",
    )


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


@router.post("/downloads")
async def presign_asset_download(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    """Presigned GET URL for preview or download of an existing asset object."""

    del app_key
    body = await _json(request)
    allowed = {"asset_path", "download_name"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise service.KnowledgeError(
            422,
            {"detail": [{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown]},
        )
    asset_path = str(body.get("asset_path") or "").strip()
    if not asset_path:
        raise service.KnowledgeError(422, {"detail": [{"loc": ["body", "asset_path"], "msg": "field required", "type": "missing"}]})
    download_name = body.get("download_name")
    download = str(download_name).strip() if download_name is not None else None
    return service.presign_download(asset_path, download or None)
