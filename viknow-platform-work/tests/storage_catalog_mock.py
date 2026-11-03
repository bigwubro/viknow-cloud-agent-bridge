"""Patch viknow-storage catalog for tests when HTTP catalog routes are removed from platform."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch


@contextmanager
def mock_storage_catalog(*, library_id: str, assets: list[dict[str, Any]]) -> Iterator[None]:
    def _require_library(lid: str) -> None:
        if lid != library_id:
            from viknow_platform.access.knowledge.service import KnowledgeError

            raise KnowledgeError(404, {"detail": "library not found"})

    def _require_asset(lid: str, asset_id: str) -> dict[str, Any]:
        _require_library(lid)
        for row in assets:
            if row.get("asset_id") == asset_id:
                return row
        from viknow_platform.access.knowledge.service import KnowledgeError

        raise KnowledgeError(404, {"detail": "asset not found"})

    def _list_assets(lid: str, limit: int, after: str | None = None) -> list[dict[str, Any]]:
        del limit, after
        _require_library(lid)
        return list(assets)

    with (
        patch("viknow_platform.access.knowledge.storage_catalog.require_library", _require_library),
        patch("viknow_platform.access.knowledge.storage_catalog.require_asset", _require_asset),
        patch("viknow_platform.access.knowledge.storage_catalog.list_assets", _list_assets),
        patch("viknow_platform.access.knowledge.storage_catalog.library_exists", lambda lid: lid == library_id),
    ):
        yield
