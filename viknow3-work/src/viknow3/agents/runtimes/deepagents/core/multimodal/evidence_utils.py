"""Shared Evidence helpers for multimodal services."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from viknow3.agents.runtimes.deepagents.core.evidence import Evidence, EvidencePart, EvidencePartKind
from viknow3.agents.runtimes.deepagents.core.knowledge.object_key import parse_storage_reference


def resolve_media_path(repo_root: Path, path_value: str) -> Path:
    path = Path(path_value)
    if parse_storage_reference(path_value) is not None:
        raise ValueError("storage artifact URIs must be read via read_evidence_image_bytes")
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def read_evidence_image_bytes(repo_root: Path, part: EvidencePart) -> tuple[bytes, str]:
    uri = (part.uri or "").strip()
    if not uri:
        raise ValueError("image evidence part requires uri")
    ref = parse_storage_reference(uri)
    if ref is not None and ref.artifact_id:
        from viknow3.service.storage.gateway import get_storage_client

        payload = get_storage_client().download_artifact(
            ref.library_id, ref.asset_id, ref.artifact_id
        )
        mime = part.mime_type or "image/png"
        return payload, mime
    image_path = resolve_media_path(repo_root, uri)
    mime = part.mime_type or _guess_image_mime(image_path)
    return image_path.read_bytes(), mime


def _guess_image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/jpeg"


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
