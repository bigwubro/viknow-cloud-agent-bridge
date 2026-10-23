"""User Interface routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from viknow3.access.auth import require_app
from viknow3.access.user import service

router = APIRouter(prefix="/api/v1/users", dependencies=[Depends(require_app)])


def _limit(value: int | None) -> int:
    if value is None:
        return 20
    return max(1, min(value, 100))


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    return parsed if isinstance(parsed, dict) else {}


@router.post("")
async def create_user(request: Request) -> dict[str, Any]:
    body = await _json(request)
    return service.create_user(body.get("display_name"))


@router.get("")
def list_users(limit: int | None = None, after: str | None = None) -> list[dict[str, Any]]:
    return service.list_users(_limit(limit), after)


@router.get("/{user_id}")
def get_user(user_id: str) -> dict[str, Any]:
    return service.get_user(user_id)


@router.patch("/{user_id}")
async def update_user(user_id: str, request: Request) -> dict[str, Any]:
    body = await _json(request)
    return service.update_user(user_id, body.get("display_name"))


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str) -> Response:
    service.delete_user(user_id)
    return Response(status_code=204)


@router.delete("/{user_id}/sessions/{session_id}", status_code=204)
def delete_session(user_id: str, session_id: str) -> Response:
    service.delete_session(user_id, session_id)
    return Response(status_code=204)
