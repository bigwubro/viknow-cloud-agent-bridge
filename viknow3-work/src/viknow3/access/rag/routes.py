"""RAG Interface routes: index, search, asset index, and media bytes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from viknow3.access.auth import AppKey, require_app
from viknow3.access.rag import service

router = APIRouter(dependencies=[Depends(require_app)])
media_router = APIRouter(prefix="/media/libraries", dependencies=[Depends(require_app)])


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    return parsed if isinstance(parsed, dict) else {}


@router.post("/api/v1/knowledge/uploads")
async def upload_object(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise service.RagError(422, {"detail": [{"loc": ["file"], "msg": "file required", "type": "missing"}]})
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise service.RagError(422, {"detail": [{"loc": ["file"], "msg": "file required", "type": "missing"}]})
    asset_id = form.get("asset_id")
    return service.upload_object(
        str(form.get("library_id") or ""),
        getattr(upload, "filename", None) or "",
        await upload.read(),
        asset_id=str(asset_id) if asset_id else None,
        content_type=getattr(upload, "content_type", None),
    )


@router.post("/api/v1/knowledge/index-jobs")
async def create_index_job(request: Request, app_key: AppKey = Depends(require_app)) -> dict[str, Any]:
    return service.create_index_job(await _json(request))


@router.get("/api/v1/knowledge/index-jobs/{job_id}")
def get_index_job(job_id: str) -> dict[str, Any]:
    return service.get_index_job(job_id)


@router.get("/api/v1/knowledge/index-jobs/{job_id}/tasks")
def list_index_job_tasks(job_id: str) -> list[dict[str, Any]]:
    return service.get_index_job_tasks(job_id)


@router.delete("/api/v1/knowledge/indexes")
async def delete_index(request: Request) -> dict[str, Any]:
    body = await _json(request)
    return service.delete_index(str(body.get("library_id") or ""), str(body.get("asset_id") or ""))


@router.post("/api/v1/knowledge/search")
async def search(request: Request) -> dict[str, Any]:
    return service.search(await _json(request))


@router.get("/api/v1/online/libraries/{library_id}/assets/{asset_id}/index")
def asset_index(library_id: str, asset_id: str) -> dict[str, Any]:
    return service.asset_index(library_id, asset_id)


@media_router.get("/{library_id}/assets/{asset_id}/video")
def media_video(library_id: str, asset_id: str, request: Request) -> Response:
    return _media(library_id, asset_id, "video", request)


@media_router.get("/{library_id}/assets/{asset_id}/document")
def media_document(library_id: str, asset_id: str, request: Request) -> Response:
    return _media(library_id, asset_id, "document", request)


@media_router.get("/{library_id}/assets/{asset_id}/images/{image_name:path}")
def media_image(library_id: str, asset_id: str, image_name: str, request: Request) -> Response:
    del image_name
    return _media(library_id, asset_id, "image", request)


def _media(library_id: str, asset_id: str, kind: str, request: Request) -> Response:
    mime, content = service.media(library_id, asset_id, kind)
    range_header = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes"}
    if range_header and range_header.startswith("bytes=") and content:
        start_text, _, end_text = range_header[6:].partition("-")
        start = int(start_text or 0)
        end = int(end_text) if end_text else len(content) - 1
        chunk = content[start : end + 1]
        headers["Content-Range"] = f"bytes {start}-{start + len(chunk) - 1}/{len(content)}"
        return Response(content=chunk, status_code=206, media_type=mime, headers=headers)
    return Response(content=content, media_type=mime, headers=headers)
