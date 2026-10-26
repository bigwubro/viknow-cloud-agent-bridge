"""OCR services and OpenAI-compatible placeholder client."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind
from agents.runtime.core.multimodal.evidence_utils import first_visual_media_part, resolve_media_path
from agents.runtime.core.multimodal.review import review_video_path


class OCRResult(BaseModel):
    """Normalized OCR result returned by OCR providers."""

    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class OCRClient(Protocol):
    """Port implemented by remote OCR provider adapters."""

    def recognize(
        self,
        media_path: Path,
        *,
        mime_type: str | None = None,
        instruction: str | None = None,
    ) -> OCRResult: ...


class EvidenceOCRReader:
    """Application service backing evidence OCR tools."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        ocr_client: OCRClient,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        self.ocr_client = ocr_client

    def recognize_evidence(
        self,
        evidence: Evidence,
        *,
        instruction: str | None = None,
    ) -> Evidence:
        part = first_visual_media_part(evidence)
        media_path = resolve_media_path(self.repo_root, part.uri or "")
        time_range = None
        if part.time_range is not None:
            time_range = (part.time_range.start_seconds, part.time_range.end_seconds)

        started = perf_counter()
        clip_started = perf_counter()
        if part.kind == EvidencePartKind.VIDEO:
            media_context = review_video_path(media_path, time_range)
        else:
            media_context = original_media_path(media_path)
        with media_context as submitted_media_path:
            clip_elapsed_seconds = perf_counter() - clip_started
            submitted_media_bytes = submitted_media_path.stat().st_size
            result = self.ocr_client.recognize(
                submitted_media_path,
                mime_type=part.mime_type,
                instruction=instruction,
            )
        elapsed_seconds = perf_counter() - started
        metadata = {
            **evidence.metadata,
            **result.metadata,
            "ocr_recognized": True,
            "original_evidence_id": evidence.id,
            "ocr_instruction": instruction,
            "ocr_elapsed_seconds": round(elapsed_seconds, 3),
            "ocr_source_kind": part.kind.value,
            "ocr_source_uri": part.uri,
            "ocr_media_submitted": True,
            "ocr_media_submitted_kind": "clip" if time_range is not None else "source",
            "ocr_source_media_bytes": media_path.stat().st_size,
            "ocr_media_submitted_bytes": submitted_media_bytes,
        }
        if time_range is not None:
            metadata["ocr_time_range"] = {
                "start_seconds": time_range[0],
                "end_seconds": time_range[1],
            }
            metadata["ocr_media_submitted_clip"] = True
            metadata["ocr_clip_extract_elapsed_seconds"] = round(clip_elapsed_seconds, 3)
        for metadata_key, client_attr in {
            "ocr_model": "model",
            "ocr_base_url": "base_url",
            "ocr_remote_media_bytes": "last_media_bytes",
            "ocr_media_encode_elapsed_seconds": "last_media_encode_elapsed_seconds",
            "ocr_request_payload_bytes": "last_request_payload_bytes",
            "ocr_http_elapsed_seconds": "last_http_elapsed_seconds",
        }.items():
            value = getattr(self.ocr_client, client_attr, None)
            if value is not None:
                metadata[metadata_key] = value
        return Evidence(
            id=f"{evidence.id}:evidence_ocr",
            source="evidence_ocr",
            title="OCR识别结果",
            parts=[
                EvidencePart(
                    kind=EvidencePartKind.TEXT,
                    text=result.text,
                    metadata={"role": "ocr_text"},
                ),
                part.model_copy(deep=True),
            ],
            metadata=metadata,
        )


@contextmanager
def original_media_path(path: Path) -> Iterator[Path]:
    yield path
