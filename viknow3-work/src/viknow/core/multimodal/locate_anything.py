"""LocateAnything visual grounding client wrappers."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


def _load_actio_locate_anything():
    try:
        return importlib.import_module("actio.locate_anything")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[4]
        actio_src = repo_root / "external" / "actio" / "src"
        if actio_src.exists() and str(actio_src) not in sys.path:
            sys.path.insert(0, str(actio_src))
        return importlib.import_module("actio.locate_anything")


_actio_locate_anything = _load_actio_locate_anything()
_ActioLocateAnythingClient = _actio_locate_anything.LocateAnythingClient


class LocateAnythingDetection(BaseModel):
    """One visual grounding detection."""

    label: str | None = None
    box: list[float] | None = None
    point: list[float] | None = None
    score: float | None = None
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocateAnythingResult(BaseModel):
    """Normalized LocateAnything response for ViKnow tools."""

    request_id: str
    query: str
    detections: list[LocateAnythingDetection] = Field(default_factory=list)
    image_width: int | None = None
    image_height: int | None = None
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocateAnythingClient(Protocol):
    """Port implemented by Actio LocateAnything client adapters."""

    def locate_image(
        self,
        media_path: Path,
        *,
        query: str,
        mode: str | None = None,
        max_results: int | None = None,
        generation_mode: str | None = None,
    ) -> LocateAnythingResult: ...

    def recent_requests(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


class ActioLocateAnythingClient(_ActioLocateAnythingClient):
    """ViKnow-compatible wrapper for Actio's LocateAnything client."""

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
