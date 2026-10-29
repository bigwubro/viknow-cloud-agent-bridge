"""Index artifacts stored under each catalog asset."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from viknow_storage.catalog import ASSET_ID, CatalogError, filename_of, get_asset
from viknow_storage.models import AssetArtifact, utcnow


def artifact_object_key(library_id: str, asset_id: str, artifact_id: str) -> str:
    return f"knowledge/libraries/{library_id}/assets/{asset_id}/artifacts/{artifact_id}"


def _check_artifact_id(artifact_id: str) -> str:
    text = str(artifact_id or "").strip()
    if ASSET_ID.fullmatch(text) is None:
        raise CatalogError(
            422,
            [{"loc": ["artifact_id"], "msg": "invalid artifact_id", "type": "value_error"}],
        )
    return text


def add_artifact(
    session: Session,
    library_id: str,
    asset_id: str,
    *,
    artifact_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> AssetArtifact:
    get_asset(session, library_id, asset_id)
    artifact_id = _check_artifact_id(artifact_id)
    if session.get(
        AssetArtifact,
        {"library_id": library_id, "asset_id": asset_id, "artifact_id": artifact_id},
    ) is not None:
        raise CatalogError(409, {"detail": "artifact_exists"})
    name = filename_of(filename)
    row = AssetArtifact(
        library_id=library_id,
        asset_id=asset_id,
        artifact_id=artifact_id,
        filename=name,
        content_type=content_type or "application/octet-stream",
        size_bytes=size_bytes,
        object_key=artifact_object_key(library_id, asset_id, artifact_id),
        created_at=utcnow(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_artifact(session: Session, library_id: str, asset_id: str, artifact_id: str) -> AssetArtifact:
    get_asset(session, library_id, asset_id)
    artifact_id = _check_artifact_id(artifact_id)
    row = session.get(
        AssetArtifact,
        {"library_id": library_id, "asset_id": asset_id, "artifact_id": artifact_id},
    )
    if row is None:
        raise CatalogError(404, {"detail": "artifact not found"})
    return row


def list_artifacts(session: Session, library_id: str, asset_id: str) -> list[AssetArtifact]:
    get_asset(session, library_id, asset_id)
    stmt = (
        select(AssetArtifact)
        .where(
            AssetArtifact.library_id == library_id,
            AssetArtifact.asset_id == asset_id,
        )
        .order_by(AssetArtifact.artifact_id)
    )
    return list(session.scalars(stmt))


def remove_artifact(session: Session, library_id: str, asset_id: str, artifact_id: str) -> AssetArtifact:
    row = get_artifact(session, library_id, asset_id, artifact_id)
    session.delete(row)
    session.commit()
    return row
