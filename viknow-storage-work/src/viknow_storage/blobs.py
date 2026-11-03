"""Object bytes. Only this module talks to MinIO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ObjectBody:
    data: bytes
    content_type: str
    start: int
    end: int
    size: int

    @property
    def partial(self) -> bool:
        return self.start != 0 or self.end != self.size - 1


class BlobStore(Protocol):
    def ensure_bucket(self) -> None: ...

    def put(self, key: str, payload: bytes, content_type: str) -> None: ...

    def get(self, key: str, start: int | None, end: int | None) -> ObjectBody: ...

    def delete(self, key: str) -> None: ...


class MemoryBlobStore:
    """In-process bytes for tests."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def ensure_bucket(self) -> None:
        return None

    def put(self, key: str, payload: bytes, content_type: str) -> None:
        self._objects[key] = (payload, content_type)

    def get(self, key: str, start: int | None, end: int | None) -> ObjectBody:
        payload, content_type = self._objects[key]
        size = len(payload)
        lo = 0 if start is None else start
        hi = size - 1 if end is None else end
        return ObjectBody(payload[lo : hi + 1], content_type, lo, hi, size)

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)


class S3BlobStore:
    """One bucket on the MinIO shipped with this service."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str) -> None:
        import boto3
        from botocore.config import Config

        if not endpoint or not access_key or not secret_key or not bucket:
            raise RuntimeError("S3 endpoint, credentials, and bucket are required")
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint.rstrip("/"),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or "us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, payload: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType=content_type or "application/octet-stream",
        )

    def get(self, key: str, start: int | None, end: int | None) -> ObjectBody:
        kwargs: dict = {"Bucket": self.bucket, "Key": key}
        if start is not None or end is not None:
            lo = 0 if start is None else start
            hi = "" if end is None else str(end)
            kwargs["Range"] = f"bytes={lo}-{hi}"
        response = self.client.get_object(**kwargs)
        payload = response["Body"].read()
        content_type = response.get("ContentType") or "application/octet-stream"
        size = int(response.get("ContentLength") or len(payload))
        content_range = response.get("ContentRange") or ""
        if content_range.startswith("bytes "):
            span, total = content_range.removeprefix("bytes ").split("/")
            lo_s, hi_s = span.split("-")
            return ObjectBody(payload, content_type, int(lo_s), int(hi_s), int(total))
        return ObjectBody(payload, content_type, 0, size - 1 if size else 0, size)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
