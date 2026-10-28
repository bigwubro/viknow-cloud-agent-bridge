"""Point Actio video_split at viknow-storage assets (library_id + asset_id)."""

from __future__ import annotations

import logging
from pathlib import Path

from viknow3.service.hyperrag.object_gateway import scratch_root
from viknow3.service.storage.gateway import get_storage_client, storage_configured

log = logging.getLogger(__name__)

_INSTALLED = False


def storage_split_media(local_path: str) -> dict[str, str] | None:
    """Map a scratch materialization path to Actio ``media.type=viknow_storage``."""

    if not storage_configured():
        return None
    path = Path(local_path).expanduser().resolve()
    root = scratch_root().resolve()
    storage_root = root / "storage"
    try:
        relative = path.relative_to(storage_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 3:
        return None
    library_id, asset_id, leaf = parts[0], parts[1], parts[2]
    if not leaf.startswith("original"):
        return None
    if not library_id.strip() or not asset_id.strip():
        return None
    try:
        if not get_storage_client().asset_exists(library_id, asset_id):
            return None
    except Exception:
        log.debug("storage asset_exists failed for %s/%s", library_id, asset_id, exc_info=True)
        return None
    return {
        "type": "viknow_storage",
        "library_id": library_id,
        "asset_id": asset_id,
        "filename": path.name,
    }


def install_storage_video_split() -> None:
    """Prefer viknow-storage references over base64 when submitting remote split jobs."""

    global _INSTALLED
    if _INSTALLED or not storage_configured():
        return
    from actio.tasks import segment as segment_module

    client_cls = getattr(segment_module, "VideoSplitSegmentClient", None) or getattr(
        segment_module, "RemoteVideoSplitSegmenter", None
    )
    if client_cls is None:
        raise RuntimeError("Actio video split client is unavailable")
    original = client_cls._media_input_for_path

    def _media_input_for_path(path, config):
        media = storage_split_media(str(path))
        if media is not None:
            return media
        return original(path, config)

    client_cls._media_input_for_path = staticmethod(_media_input_for_path)
    _INSTALLED = True
