"""Safe projection of indexed video chunks for the Online index detail page."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndexVisualizationItem(BaseModel):
    """One user-visible, seekable item in a video's index timeline."""

    model_config = ConfigDict(extra="forbid")

    id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    type: Literal["caption", "transcript"]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> IndexVisualizationItem:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class IndexVisualizationService:
    """Project raw indexed chunks into the public Online timeline contract."""

    _TRANSCRIPT_KINDS = frozenset({"asr", "transcript", "speech"})

    def project_chunks(
        self,
        *,
        library_id: str,
        asset_id: str,
        chunks: list[dict[str, Any]],
    ) -> list[IndexVisualizationItem]:
        projected: list[tuple[float, float, int, str, str]] = []
        for source_order, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            time_range = self._time_range(chunk)
            if time_range is None:
                continue
            start_seconds, end_seconds = time_range
            text, item_type = self._display_text(chunk)
            if not text:
                continue
            projected.append((start_seconds, end_seconds, source_order, text, item_type))

        projected.sort(key=lambda item: (item[0], item[1], item[2]))
        return [
            IndexVisualizationItem(
                id=self._opaque_item_id(
                    library_id=library_id,
                    asset_id=asset_id,
                    ordinal=ordinal,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=text,
                ),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                type=item_type,
                text=text,
            )
            for ordinal, (start_seconds, end_seconds, _, text, item_type) in enumerate(projected)
        ]

    @classmethod
    def _time_range(cls, chunk: dict[str, Any]) -> tuple[float, float] | None:
        metadata = chunk.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        raw_range = metadata.get("time_range")
        if isinstance(raw_range, dict):
            raw_start = raw_range.get("start_seconds")
            raw_end = raw_range.get("end_seconds")
        elif isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
            raw_start, raw_end = raw_range
        else:
            raw_start = metadata.get("begin_ts")
            raw_end = metadata.get("end_ts")
        start = cls._finite_float(raw_start)
        end = cls._finite_float(raw_end)
        if start is None or end is None or start < 0 or end <= start:
            return None
        return start, end

    @classmethod
    def _display_text(cls, chunk: dict[str, Any]) -> tuple[str, Literal["caption", "transcript"]]:
        metadata = chunk.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if str(metadata.get("chunk_type") or "").lower() == "video":
            return cls._video_display_text(chunk.get("content"), metadata), "caption"

        content = chunk.get("content")
        if isinstance(content, str) and content:
            return content, "caption"

        for key in ("caption", "summary"):
            text = metadata.get(key)
            if isinstance(text, str) and text:
                return text, "caption"

        candidates = metadata.get("candidate_evidence")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                kind = str(candidate.get("kind") or "").strip().lower()
                if kind in cls._TRANSCRIPT_KINDS:
                    text = candidate.get("text")
                    if isinstance(text, str) and text:
                        return text, "transcript"

        return "", "caption"

    @staticmethod
    def _video_display_text(content: Any, metadata: dict[str, Any]) -> str:
        context = str(metadata.get("context") or "")
        if not context:
            context = IndexVisualizationService._legacy_context_from_content(content)

        fields = {
            "context": context,
            "chunk_title": str(metadata.get("chunk_title") or ""),
            "caption": str(metadata.get("caption") or ""),
            "object": str(metadata.get("object") or ""),
            "ocr": str(metadata.get("ocr") or ""),
            "asr": str(metadata.get("asr") or ""),
        }
        return json.dumps(fields, ensure_ascii=False)

    @staticmethod
    def _legacy_context_from_content(content: Any) -> str:
        if isinstance(content, str):
            try:
                parsed_content = json.loads(content)
            except json.JSONDecodeError:
                parsed_content = None
            if isinstance(parsed_content, dict):
                return str(parsed_content.get("context") or "")

            context_marker = "### context"
            chunk_marker = "### chunk text"
            if context_marker in content:
                context = content.split(context_marker, 1)[1]
                if chunk_marker in context:
                    context = context.split(chunk_marker, 1)[0]
                context = context.strip()
                if len(context) >= 2 and context[0] in '"“' and context[-1] in '"”':
                    context = context[1:-1]
                return context.strip()
        return ""

    @staticmethod
    def _finite_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result and abs(result) != float("inf") else None

    @staticmethod
    def _opaque_item_id(
        *,
        library_id: str,
        asset_id: str,
        ordinal: int,
        start_seconds: float,
        end_seconds: float,
        text: str,
    ) -> str:
        payload = "\0".join(
            (
                library_id,
                asset_id,
                str(ordinal),
                f"{start_seconds:.6f}",
                f"{end_seconds:.6f}",
                text,
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"timeline-{digest}"
