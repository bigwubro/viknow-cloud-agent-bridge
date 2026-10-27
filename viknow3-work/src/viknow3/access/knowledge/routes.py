"""Knowledge HTTP routes.

Playground uploads libraries/assets to viknow-storage, then calls index-jobs with
library_id + asset_id only. ``/downloads`` accepts library_id+asset_id; legacy asset_path
presign is only for old ``knowledge/assets/`` citations.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile

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


@router.get("/libraries/{library_id}/assets/{asset_id}/preview")
def preview_storage_asset(
    library_id: str,
    asset_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
    app_key: AppKey = Depends(require_app),
) -> Response:
    del app_key
    status, headers, body = service.proxy_storage_asset(
        library_id, asset_id, preview=True, range_header=range_header
    )
    return Response(content=body, status_code=status, headers=headers)


@router.get("/libraries/{library_id}/assets/{asset_id}/content")
def download_storage_asset(
    library_id: str,
    asset_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
    app_key: AppKey = Depends(require_app),
) -> Response:
    del app_key
    status, headers, body = service.proxy_storage_asset(
        library_id, asset_id, preview=False, range_header=range_header
    )
    return Response(content=body, status_code=status, headers=headers)


@router.post("/downloads")
async def presign_asset_download(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    """Presigned GET URL for preview or download of an existing asset object."""

    del app_key
    body = await _json(request)
    allowed = {"library_id", "asset_id", "download", "asset_path", "download_name"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        raise service.KnowledgeError(
            422,
            {"detail": [{"loc": ["body", key], "msg": "extra fields not permitted", "type": "extra_forbidden"} for key in unknown]},
        )
    library_id = str(body.get("library_id") or "").strip()
    asset_id = str(body.get("asset_id") or "").strip()
    if library_id and asset_id:
        return service.asset_content_url(library_id, asset_id, download=bool(body.get("download")))
    asset_path = str(body.get("asset_path") or "").strip()
    if not asset_path:
        raise service.KnowledgeError(
            422,
            {"detail": [{"loc": ["body"], "msg": "library_id and asset_id are required", "type": "missing"}]},
        )
    download_name = body.get("download_name")
    download = str(download_name).strip() if download_name is not None else None
    return service.presign_download(asset_path, download or None)


@router.post("/uploads")
async def upload_object(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise service.KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file required", "type": "missing"}]})
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise service.KnowledgeError(422, {"detail": [{"loc": ["file"], "msg": "file required", "type": "missing"}]})
    asset_id = form.get("asset_id")
    return service.upload_object(
        str(form.get("library_id") or ""),
        getattr(upload, "filename", None) or "",
        await upload.read(),
        asset_id=str(asset_id) if asset_id else None,
        content_type=getattr(upload, "content_type", None),
    )


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
