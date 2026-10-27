"""Catalog object keys the playground can sign. These are storage keys, not media URLs."""

from __future__ import annotations

from typing import Any

_PREFIX = "knowledge/assets/"
_CACHE: dict[tuple[str, str], str] = {}


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
    key = knowledge_object_key(getattr(row, "asset_path", None))
    if key:
        _CACHE[(library, asset)] = key
    return key


def resolve_asset_object_key(
    library_id: str | None,
    asset_id: str | None,
    *candidates: Any,
) -> str | None:
    """Prefer an object key already on the payload, then the catalog row."""

    for candidate in candidates:
        key = knowledge_object_key(candidate)
        if key:
            return key
    return catalog_asset_object_key(str(library_id or ""), str(asset_id or ""))
