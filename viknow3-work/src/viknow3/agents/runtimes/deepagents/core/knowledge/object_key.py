"""Logical asset references for citations and inspect (``storage://`` or legacy S3 keys)."""

from __future__ import annotations

from typing import Any

from viknow3.service.storage.gateway import storage_configured

_PREFIX = "knowledge/assets/"
_STORAGE_PREFIX = "storage://"
_CACHE: dict[tuple[str, str], str] = {}


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


def knowledge_object_key(value: Any) -> str | None:
    """Return ``value`` when it is a ``knowledge/assets/`` object key."""

    if not isinstance(value, str):
        return None
    text = value.strip().split("#", 1)[0].split("?", 1)[0].lstrip("/")
    if not text.startswith(_PREFIX):
        return None
    if any(part in {"", ".", ".."} for part in text.split("/")):
        return None
    return text


def catalog_asset_object_key(library_id: str, asset_id: str) -> str | None:
    """Look up the asset row's ``asset_path``. Missing config or rows yield None."""

    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    if not library or not asset:
        return None
    cached = _CACHE.get((library, asset))
    if cached:
        return cached
    try:
        from viknow3.access.knowledge.store import get_asset
    except Exception:
        return None
    try:
        row = get_asset(library, asset)
    except Exception:
        return None
    if row is None:
        return None
    path = getattr(row, "asset_path", None)
    key = knowledge_object_key(path)
    if key:
        _CACHE[(library, asset)] = key
        return key
    parsed = parse_storage_uri(path)
    if parsed and parsed == (library, asset):
        uri = storage_uri(library, asset)
        _CACHE[(library, asset)] = uri
        return uri
    return None


def resolve_asset_object_key(
    library_id: str | None,
    asset_id: str | None,
    *candidates: Any,
) -> str | None:
    """Prefer ``storage://`` when configured, then legacy ``knowledge/assets/`` keys."""

    library = str(library_id or "").strip()
    asset = str(asset_id or "").strip()
    for candidate in candidates:
        parsed = parse_storage_uri(candidate)
        if parsed:
            return storage_uri(parsed[0], parsed[1])
        key = knowledge_object_key(candidate)
        if key:
            return key
    if library and asset and storage_configured():
        return storage_uri(library, asset)
    return catalog_asset_object_key(library, asset)
