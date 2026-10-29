"""Knowledge HTTP routes — index status only; catalog is viknow-storage."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from viknow_platform.access.auth import AppKey, require_app
from viknow_platform.access.knowledge import service

router = APIRouter(prefix="/api/v1/knowledge", dependencies=[Depends(require_app)])


@router.get("/libraries/{library_id}/assets/index-status-batch")
def library_assets_index_status_batch(
    library_id: str,
    limit: int | None = None,
    app_key: AppKey = Depends(require_app),
) -> list[dict[str, object]]:
    del app_key
    return service.list_library_assets_index_status(library_id, limit)


@router.get("/libraries/{library_id}/assets/{asset_id}/index-status")
def asset_index_status(
    library_id: str,
    asset_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, object]:
    del app_key
    return service.get_asset_index_status(library_id, asset_id)


@router.get("/libraries/{library_id}/assets/{asset_id}/index-jobs/latest")
def latest_asset_index_job(
    library_id: str,
    asset_id: str,
    app_key: AppKey = Depends(require_app),
) -> dict[str, object]:
    del app_key
    return service.get_latest_asset_index_job(library_id, asset_id)
