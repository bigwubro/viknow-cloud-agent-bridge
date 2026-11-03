"""Library and asset records."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from viknow_storage.models import Asset, Library, utcnow

LIBRARY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
ASSET_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")

MODALITY = {
    ".pdf": "document",
    ".txt": "document",
    ".md": "document",
    ".markdown": "document",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


class CatalogError(Exception):
    def __init__(self, status: int, detail) -> None:
        self.status = status
        self.detail = detail


def object_key(library_id: str, asset_id: str) -> str:
    return f"knowledge/libraries/{library_id}/assets/{asset_id}/original"


def _limit(value: int | None) -> int:
    if value is None:
        return 20
    return max(1, min(int(value), 100))


def _check_library_id(library_id: str) -> str:
    text = str(library_id or "").strip()
    if LIBRARY_ID.fullmatch(text) is None:
        raise CatalogError(
            422,
            [{"loc": ["library_id"], "msg": "invalid library_id", "type": "value_error"}],
        )
    return text


def _check_asset_id(asset_id: str) -> str:
    text = str(asset_id or "").strip()
    if ASSET_ID.fullmatch(text) is None:
        raise CatalogError(
            422,
            [{"loc": ["asset_id"], "msg": "invalid asset_id", "type": "value_error"}],
        )
    return text


def filename_of(raw: str) -> str:
    name = PurePosixPath(str(raw or "").replace("\\", "/")).name
    if not name or name in {".", ".."} or '"' in name or "\n" in name or "\r" in name:
        raise CatalogError(422, [{"loc": ["file"], "msg": "file name is required", "type": "missing"}])
    if SUFFIX.fullmatch(PurePosixPath(name).suffix.lower()) is None:
        raise CatalogError(
            422,
            [{"loc": ["file"], "msg": "file extension is required", "type": "value_error"}],
        )
    return name


def modality_of(filename: str) -> str:
    return MODALITY.get(PurePosixPath(filename).suffix.lower(), "document")


def create_library(session: Session, library_id: str, title: str | None) -> Library:
    library_id = _check_library_id(library_id)
    title_text = str(title).strip() if title is not None and str(title).strip() else library_id
    row = session.get(Library, library_id)
    now = utcnow()
    if row is None:
        row = Library(library_id=library_id, title=title_text, created_at=now, updated_at=now, deleted=False)
        session.add(row)
    elif row.deleted:
        row.deleted = False
        row.title = title_text
        row.updated_at = now
    else:
        raise CatalogError(409, {"detail": "library_exists"})
    session.commit()
    session.refresh(row)
    return row


def get_library(session: Session, library_id: str) -> Library:
    library_id = _check_library_id(library_id)
    row = session.get(Library, library_id)
    if row is None or row.deleted:
        raise CatalogError(404, {"detail": "library not found"})
    return row


def list_libraries(session: Session, limit: int | None, after: str | None) -> list[Library]:
    stmt = select(Library).where(Library.deleted.is_(False)).order_by(Library.library_id).limit(_limit(limit))
    cursor = str(after or "").strip()
    if cursor:
        stmt = stmt.where(Library.library_id > cursor)
    return list(session.scalars(stmt))


def delete_library(session: Session, library_id: str) -> None:
    row = get_library(session, library_id)
    row.deleted = True
    row.updated_at = utcnow()
    session.commit()


def get_asset(session: Session, library_id: str, asset_id: str) -> Asset:
    get_library(session, library_id)
    asset_id = _check_asset_id(asset_id)
    row = session.get(Asset, {"library_id": library_id, "asset_id": asset_id})
    if row is None:
        raise CatalogError(404, {"detail": "asset not found"})
    return row


def list_assets(session: Session, library_id: str, limit: int | None, after: str | None) -> list[Asset]:
    get_library(session, library_id)
    stmt = (
        select(Asset)
        .where(Asset.library_id == library_id)
        .order_by(Asset.asset_id)
        .limit(_limit(limit))
    )
    cursor = str(after or "").strip()
    if cursor:
        stmt = stmt.where(Asset.asset_id > cursor)
    return list(session.scalars(stmt))


def add_asset(
    session: Session,
    library_id: str,
    *,
    asset_id: str | None,
    filename: str,
    content_type: str,
    size_bytes: int,
    title: str | None,
) -> Asset:
    get_library(session, library_id)
    chosen = _check_asset_id(asset_id) if asset_id else str(uuid4())
    if session.get(Asset, {"library_id": library_id, "asset_id": chosen}) is not None:
        raise CatalogError(409, {"detail": "asset_exists"})
    name = filename_of(filename)
    title_text = str(title).strip() if title is not None and str(title).strip() else name
    row = Asset(
        library_id=library_id,
        asset_id=chosen,
        title=title_text,
        modality=modality_of(name),
        filename=name,
        content_type=content_type or "application/octet-stream",
        size_bytes=size_bytes,
        object_key=object_key(library_id, chosen),
        created_at=utcnow(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def remove_asset(session: Session, library_id: str, asset_id: str) -> Asset:
    row = get_asset(session, library_id, asset_id)
    session.delete(row)
    session.commit()
    return row
