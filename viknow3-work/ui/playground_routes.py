"""Playground: no business BFF. Legacy /ui/playground/* routes stay removed (404)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/ui/playground", tags=["playground"])
