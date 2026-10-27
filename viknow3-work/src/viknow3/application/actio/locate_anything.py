"""LocateAnything client backed by the actio package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.runtime.core.multimodal.locate_anything import (
    LocateAnythingDetection,
    LocateAnythingResult,
)
from viknow3.application.actio._load import load_actio

_ActioLocateAnythingClient = load_actio("actio.locate_anything").LocateAnythingClient


class ActioLocateAnythingClient(_ActioLocateAnythingClient):
    """LocateAnything client that returns runtime result models."""

    def locate_image(
        self,
        media_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult:
        result = super().locate_image(
            media_path,
            query=query,
            mode=mode,
            max_results=max_results,
            generation_mode=generation_mode,
        )
        return _to_result(result)

    def locate_base64(
        self,
        image_base64: str,
        *,
        query: str,
        filename: str | None = None,
        mime_type: str | None = None,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult:
        result = super().locate_base64(
            image_base64,
            query=query,
            filename=filename,
            mime_type=mime_type,
            mode=mode,
            max_results=max_results,
            generation_mode=generation_mode,
        )
        return _to_result(result)

    def locate_path(
        self,
        image_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult:
        result = super().locate_path(
            image_path,
            query=query,
            mode=mode,
            max_results=max_results,
            generation_mode=generation_mode,
        )
        return _to_result(result)

    def locate_path_async(
        self,
        media_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult:
        result = super().locate_path_async(
            media_path,
            query=query,
            mode=mode,
            max_results=max_results,
            generation_mode=generation_mode,
        )
        return _to_result(result)


def _to_result(result: Any) -> LocateAnythingResult:
    return LocateAnythingResult(
        request_id=result.request_id,
        query=result.query,
        detections=[
            LocateAnythingDetection(
                label=item.label,
                box=item.box,
                point=item.point,
                score=item.score,
                text=item.text,
                metadata=item.metadata,
            )
            for item in result.detections
        ],
        image_width=result.image_width,
        image_height=result.image_height,
        text=result.text,
        metadata=result.metadata,
    )
