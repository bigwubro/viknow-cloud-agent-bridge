"""Playground: no business BFF. Browser calls /api/v1/* (see playground_access mount)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/ui/playground", tags=["playground"])
