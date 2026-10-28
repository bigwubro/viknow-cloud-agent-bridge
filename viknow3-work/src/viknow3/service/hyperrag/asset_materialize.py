"""Resolve asset references to local POSIX paths for HyperRAG ingest and Actio inspect."""

from __future__ import annotations

from pathlib import Path

from viknow3.agents.runtimes.deepagents.core.knowledge.object_key import parse_storage_uri, storage_uri
from viknow3.service.hyperrag.object_gateway import fetch_index_asset
from viknow3.service.storage.gateway import storage_configured


def materialize_asset_reference(
    reference: str | None,
    *,
    library_id: str = "",
    asset_id: str = "",
) -> Path | None:
    """Turn ``storage://`` or an existing absolute file path into a local file."""

    text = str(reference or "").strip()
    if not text and library_id and asset_id and storage_configured():
        text = storage_uri(library_id, asset_id)
    if not text:
        return None
    parsed = parse_storage_uri(text)
    if parsed:
        return fetch_index_asset(parsed[0], parsed[1]).path
    path = Path(text)
    if path.is_file():
        return path
    return None


def materialize_asset_reference_str(
    reference: str | None,
    *,
    library_id: str = "",
    asset_id: str = "",
) -> str | None:
    path = materialize_asset_reference(reference, library_id=library_id, asset_id=asset_id)
    return str(path) if path is not None else (str(reference).strip() or None)
