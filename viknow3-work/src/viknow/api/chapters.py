"""Explicit ViCoach learning API; the artifact itself is a plain list."""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from viknow.capabilities.vicoach.chapters.export import LearningChapter


class ChapterJobResponse(BaseModel):
    status: Literal["not_generated", "queued", "running", "completed", "failed", "stale"]
    job_key: str | None = None
    source_sha256: str | None = None
    chapters: list[LearningChapter] = Field(default_factory=list)
    error: str | None = None


def create_chapter_router(resolve_service: Callable) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/coach/libraries/{library_id}/assets/{asset_id}/chapters", tags=["coach"]
    )

    @router.get("", response_model=ChapterJobResponse)
    def read(request: Request, library_id: str, asset_id: str):
        try:
            return resolve_service(request).read(library_id, asset_id)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get("/charpter.json", response_model=list[LearningChapter])
    def download(request: Request, library_id: str, asset_id: str):
        result = read(request, library_id, asset_id)
        if result["status"] != "completed":
            raise HTTPException(409, "learning chapters are not ready or source has changed")
        return JSONResponse(
            result["chapters"], headers={"Content-Disposition": 'inline; filename="charpter.json"'}
        )

    @router.post("", response_model=ChapterJobResponse, status_code=202)
    async def generate(request: Request, library_id: str, asset_id: str):
        try:
            return await resolve_service(request).generate(library_id, asset_id)
        except KeyError as exc:
            raise HTTPException(404, "asset not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    return router
