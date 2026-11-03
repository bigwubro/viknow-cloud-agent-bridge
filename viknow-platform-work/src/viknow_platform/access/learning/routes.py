"""Self Learning routes. The learning service design is still open."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from viknow_platform.access.auth import require_app
from viknow_platform.access.learning import service

router = APIRouter(prefix="/api/v1/learning", dependencies=[Depends(require_app)])


@router.get("")
def learning_status() -> dict[str, Any]:
    return service.status()
