"""OCR clients backed by the actio package."""

from __future__ import annotations

from pathlib import Path

from agents.runtime.core.multimodal.ocr import OCRResult
from viknow3.application.actio._load import load_actio

_actio_ocr = load_actio("actio.ocr")
_ActioOpenAICompatibleOCRClient = _actio_ocr.OpenAICompatibleOCRClient
_ActioPaddleOCRClient = _actio_ocr.PaddleOCRClient


class OpenAICompatibleOCRClient(_ActioOpenAICompatibleOCRClient):
    """OpenAI-compatible OCR that returns the runtime OCR result."""

    def recognize(
        self,
        media_path: Path,
        *,
        mime_type: str | None = None,
        instruction: str | None = None,
    ) -> OCRResult:
        result = super().recognize(
            media_path,
            mime_type=mime_type,
            instruction=instruction,
        )
        return OCRResult(text=result.text, metadata=result.metadata)


class PaddleOCRClient(_ActioPaddleOCRClient):
    """Paddle OCR that returns the runtime OCR result."""

    def recognize(
        self,
        media_path: Path,
        *,
        mime_type: str | None = None,
        instruction: str | None = None,
    ) -> OCRResult:
        result = super().recognize(
            media_path,
            mime_type=mime_type,
            instruction=instruction,
        )
        return OCRResult(text=result.text, metadata=result.metadata)
