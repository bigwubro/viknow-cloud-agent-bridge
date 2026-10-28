"""Ops S3 client helpers (bucket CORS / admin scripts only — not the Playground upload path)."""

from __future__ import annotations

import os
from typing import Any

from viknow3.ops.config import store as ops_store


class StorageError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def _cfg() -> dict[str, Any]:
    if not ops_store.configured():
        raise StorageError(503, "ops storage is not configured")
    try:
        return ops_store.plain_singleton("s3_client")
    except Exception as exc:
        raise StorageError(503, "s3_client config is unavailable") from exc


def _client(*, endpoint_url: str | None = None):
    import boto3
    from botocore.config import Config

    cfg = _cfg()
    endpoint = endpoint_url or cfg.get("endpoint") or ""
    access_key = cfg.get("access_key") or ""
    secret_key = cfg.get("secret_key") or ""
    region = cfg.get("region") or "us-east-1"
    if not endpoint or not access_key or not secret_key:
        raise StorageError(503, "s3_client credentials are incomplete")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    return client, cfg


def _signing_endpoint(cfg: dict[str, Any]) -> str:
    """Browser-facing host must match the endpoint used when signing."""
    env = os.environ.get("VIKNOW_S3_PUBLIC_ENDPOINT", "").strip().rstrip("/")
    if env:
        return env
    return (cfg.get("public_endpoint") or cfg.get("endpoint") or "").rstrip("/")
