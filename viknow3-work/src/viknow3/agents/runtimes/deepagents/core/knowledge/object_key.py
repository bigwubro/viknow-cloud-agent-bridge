"""Logical asset references for citations and inspect (``storage://`` only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote

from viknow3.service.storage.gateway import storage_configured

_STORAGE_PREFIX = "storage://"


@dataclass(frozen=True)
class StorageReference:
    library_id: str
    asset_id: str
    artifact_id: str | None = None


def storage_uri(library_id: str, asset_id: str) -> str:
    return f"{_STORAGE_PREFIX}{library_id}/{asset_id}"


def parse_storage_reference(value: Any) -> StorageReference | None:
    if not isinstance(value, str) or not value.startswith(_STORAGE_PREFIX):
        return None
    tail = value[len(_STORAGE_PREFIX) :]
    path_part, sep, query_part = tail.partition("?")
    if not path_part or "/" not in path_part:
        return None
    library_id, asset_id = path_part.split("/", 1)
    library_id = library_id.strip()
    asset_id = asset_id.strip()
    if not library_id or not asset_id or ".." in library_id or ".." in asset_id:
        return None
    artifact_id: str | None = None
    if sep:
        params = parse_qs(query_part, keep_blank_values=False)
        raw = params.get("artifact", [None])[0]
        if raw:
            artifact_id = unquote(str(raw).strip()) or None
    return StorageReference(library_id=library_id, asset_id=asset_id, artifact_id=artifact_id)


def parse_storage_uri(value: Any) -> tuple[str, str] | None:
    ref = parse_storage_reference(value)
    if ref is None:
        return None
    return ref.library_id, ref.asset_id


def catalog_asset_object_key(library_id: str, asset_id: str) -> str | None:
    """Resolve ``storage://`` for a library/asset pair when storage is configured."""

    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    if not library or not asset:
        return None
    if storage_configured():
        return storage_uri(library, asset)
    return None


def resolve_asset_object_key(
    library_id: str | None,
    asset_id: str | None,
    *candidates: Any,
) -> str | None:
    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    for candidate in candidates:
        parsed = parse_storage_uri(candidate)
        if parsed:
            return storage_uri(parsed[0], parsed[1])
        ref = parse_storage_reference(candidate)
        if ref is not None and ref.artifact_id:
            from viknow3.service.storage.inspect_artifacts import artifact_storage_uri

            return artifact_storage_uri(ref.library_id, ref.asset_id, ref.artifact_id)
    if library and asset and storage_configured():
        return storage_uri(library, asset)
    return catalog_asset_object_key(library, asset)
