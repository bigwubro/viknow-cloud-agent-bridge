"""Library and asset rows. The object key is not part of the public record."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Integer, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CATALOG_SCHEMA = "storage"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(schema=CATALOG_SCHEMA)


class Library(Base):
    __tablename__ = "libraries"

    library_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def public(self) -> dict:
        return {
            "library_id": self.library_id,
            "title": self.title,
            "created_at": _stamp(self.created_at),
            "updated_at": _stamp(self.updated_at),
        }


class Asset(Base):
    __tablename__ = "assets"

    library_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    modality: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def public(self) -> dict:
        return {
            "library_id": self.library_id,
            "asset_id": self.asset_id,
            "title": self.title,
            "modality": self.modality,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "created_at": _stamp(self.created_at),
        }


class AssetArtifact(Base):
    """Index pipeline output attached to a catalog asset (not the original upload)."""

    __tablename__ = "asset_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "asset_id"],
            ["assets.library_id", "assets.asset_id"],
        ),
    )

    library_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def public(self) -> dict:
        return {
            "library_id": self.library_id,
            "asset_id": self.asset_id,
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "created_at": _stamp(self.created_at),
        }


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
