"""Shared Evidence helpers for multimodal services."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agents.runtime.core.evidence import Evidence, EvidencePart, EvidencePartKind


def resolve_media_path(repo_root: Path, path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def first_part_with_uri(
    evidence: Evidence,
    kinds: Sequence[EvidencePartKind],
    *,
    missing_message: str,
    uri_message: str,
) -> EvidencePart:
    for kind in kinds:
        for part in evidence.parts:
            if part.kind == kind:
                if not part.uri:
                    raise ValueError(uri_message)
                return part
    raise ValueError(missing_message)


def first_video_part(
    evidence: Evidence,
    *,
    missing_message: str = "evidence does not contain a video part",
    uri_message: str = "video evidence part requires uri",
) -> EvidencePart:
    return first_part_with_uri(
        evidence,
        [EvidencePartKind.VIDEO],
        missing_message=missing_message,
        uri_message=uri_message,
    )


def first_media_part(evidence: Evidence) -> EvidencePart:
    return first_part_with_uri(
        evidence,
        [EvidencePartKind.VIDEO, EvidencePartKind.FILE],
        missing_message="evidence does not contain a video or file part",
        uri_message="media evidence part requires uri",
    )


def first_visual_media_part(evidence: Evidence) -> EvidencePart:
    return first_part_with_uri(
        evidence,
        [EvidencePartKind.IMAGE, EvidencePartKind.VIDEO, EvidencePartKind.FILE],
        missing_message="evidence does not contain an image, video, or file part",
        uri_message="ocr evidence part requires uri",
    )


def first_text(evidence: Evidence) -> str:
    for part in evidence.parts:
        if part.kind == EvidencePartKind.TEXT and part.text:
            return part.text.strip()
    return ""
