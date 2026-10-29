"""OM Interface routes. Admin credential only."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from viknow_platform.access.auth import require_admin
from viknow_platform.access.om import service

router = APIRouter(prefix="/api/v1/om", dependencies=[Depends(require_admin)])


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    return parsed if isinstance(parsed, dict) else {}


@router.get("/config")
def get_config() -> dict[str, Any]:
    return service.get_config()


@router.post("/config")
async def update_config(request: Request) -> dict[str, Any]:
    body = await _json(request)
    row_id = body.get("id")
    return service.set_config(
        str(body.get("table") or ""),
        str(body.get("column") or ""),
        body.get("value"),
        None if row_id is None else str(row_id),
    )


@router.get("/config/changes")
def list_changes() -> dict[str, Any]:
    return service.changes()


@router.post("/config/changes/{change_id}/revert")
def revert_change(change_id: str) -> dict[str, Any]:
    return service.revert(change_id)
