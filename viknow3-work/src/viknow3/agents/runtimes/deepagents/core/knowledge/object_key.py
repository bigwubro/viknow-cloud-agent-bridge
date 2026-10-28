"""Logical asset references for citations and inspect (``storage://`` only)."""

from __future__ import annotations

from typing import Any

from viknow3.service.storage.gateway import storage_configured

_STORAGE_PREFIX = "storage://"


def storage_uri(library_id: str, asset_id: str) -> str:
    return f"{_STORAGE_PREFIX}{library_id}/{asset_id}"


def parse_storage_uri(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith(_STORAGE_PREFIX):
        return None
    rest = value[len(_STORAGE_PREFIX) :].strip()
    if not rest or "/" not in rest:
        return None
    library_id, asset_id = rest.split("/", 1)
    library_id = library_id.strip()
    asset_id = asset_id.strip()
    if not library_id or not asset_id or ".." in library_id or ".." in asset_id:
        return None
    return library_id, asset_id


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
    if library and asset and storage_configured():
        return storage_uri(library, asset)
    return catalog_asset_object_key(library, asset)
