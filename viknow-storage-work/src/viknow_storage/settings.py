"""Process settings. Object-store credentials never leave this service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    database_url: str
    blob_backend: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_region: str
    host: str
    port: int
    cors_origins: tuple[str, ...]
    grant_secret: str
    download_base_url: str
    grant_ttl_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        ttl_raw = os.environ.get("STORAGE_GRANT_TTL_SECONDS", "86400").strip() or "86400"
        try:
            grant_ttl = int(ttl_raw)
        except ValueError:
            grant_ttl = 86400
        return cls(
            token=os.environ.get("STORAGE_TOKEN", "").strip(),
            database_url=os.environ.get("STORAGE_DATABASE_URL", "").strip(),
            blob_backend=os.environ.get("STORAGE_BLOB", "s3").strip() or "s3",
            s3_endpoint=os.environ.get("S3_ENDPOINT", "").strip(),
            s3_access_key=os.environ.get("S3_ACCESS_KEY", "").strip(),
            s3_secret_key=os.environ.get("S3_SECRET_KEY", "").strip(),
            s3_bucket=os.environ.get("S3_BUCKET", "viknow").strip() or "viknow",
            s3_region=os.environ.get("S3_REGION", "us-east-1").strip() or "us-east-1",
            host=os.environ.get("STORAGE_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=int(os.environ.get("STORAGE_PORT", "8080")),
            cors_origins=_parse_cors_origins(os.environ.get("STORAGE_CORS_ORIGINS", "")),
            grant_secret=os.environ.get("STORAGE_GRANT_SECRET", "").strip(),
            download_base_url=os.environ.get("STORAGE_DOWNLOAD_BASE_URL", "").strip(),
            grant_ttl_seconds=max(1, min(grant_ttl, 86400)),
        )


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())

