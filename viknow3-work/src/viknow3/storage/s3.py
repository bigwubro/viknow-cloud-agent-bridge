"""S3 gateway helpers using viknow_ops s3_client (Postgres-backed config)."""

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


def _internal_endpoint(cfg: dict[str, Any]) -> str:
    return (cfg.get("endpoint") or "").rstrip("/")


def object_key(asset_path: str) -> str:
    path = asset_path.strip().lstrip("/")
    if not path.startswith("knowledge/assets/"):
        raise StorageError(400, "asset_path must start with knowledge/assets/")
    return path


def head_object(asset_path: str) -> dict[str, Any]:
    cfg = _cfg()
    client, cfg = _client(endpoint_url=_internal_endpoint(cfg))
    bucket = cfg.get("bucket") or ""
    if not bucket:
        raise StorageError(503, "s3 bucket is not configured")
    key = object_key(asset_path)
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise StorageError(400, "asset_path does not exist or is not a file") from exc
        raise StorageError(400, "asset_path is not an available storage object key or absolute file path") from exc
    except Exception as exc:
        raise StorageError(400, "asset_path is not an available storage object key or absolute file path") from exc


def presign_put(asset_path: str, content_type: str) -> dict[str, Any]:
    cfg = _cfg()
    client, cfg = _client(endpoint_url=_signing_endpoint(cfg))
    bucket = cfg.get("bucket") or ""
    expires = int(cfg.get("url_expires_seconds") or 3600)
    key = object_key(asset_path)
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )
    return {"method": "PUT", "url": url, "asset_path": key, "expires_in": expires, "content_type": content_type}


def presign_get(asset_path: str, *, download_name: str | None = None) -> dict[str, Any]:
    cfg = _cfg()
    client, cfg = _client(endpoint_url=_signing_endpoint(cfg))
    bucket = cfg.get("bucket") or ""
    expires = int(cfg.get("url_expires_seconds") or 3600)
    key = object_key(asset_path)
    params: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    url = client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
    return {"method": "GET", "url": url, "asset_path": key, "expires_in": expires}
