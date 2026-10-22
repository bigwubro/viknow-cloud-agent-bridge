"""Self Learning routes. The learning service design is still open."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from viknow3.access.auth import require_app
from viknow3.access.learning import service

router = APIRouter(prefix="/api/v1/learning", dependencies=[Depends(require_app)])


@router.get("")
def learning_status() -> dict[str, Any]:
    return service.status()
