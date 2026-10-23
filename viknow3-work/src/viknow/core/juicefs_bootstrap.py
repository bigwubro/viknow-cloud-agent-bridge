"""Bootstrap JuiceFS infra prerequisites shared by app startup and runtime mount.

Container entrypoint runs ``juicefs_runtime`` for schema/bucket/format/mount.
Independent of ``VIKNOW_PERSISTENCE_BACKEND`` (session may use sqlite).
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus, urlparse

from loguru import logger
from minio import Minio

from viknow.core.config import PostgresPersistenceConfig

JUICEFS_DATA_BUCKET = "juicefs-viknow"
JUICEFS_VOLUME_NAME = "viknow"
JUICEFS_VOLUME_NAME_ENV = "JUICEFS_VOLUME_NAME"
JUICEFS_STORAGE_ENV = "JUICEFS_STORAGE"
JUICEFS_BUCKET_URL_ENV = "JUICEFS_BUCKET_URL"
JUICEFS_DATA_BUCKET_ENV = "JUICEFS_DATA_BUCKET"
_MINIO_ENDPOINT_ENV = "MINIO_ENDPOINT"
_MINIO_ACCESS_KEY_ENV = "MINIO_ACCESS_KEY"
_MINIO_SECRET_KEY_ENV = "MINIO_SECRET_KEY"
_OSS_ENDPOINT_ENV = "OSS_ENDPOINT"
_OSS_ACCESS_KEY_ID_ENV = "OSS_ACCESS_KEY_ID"
_OSS_SECRET_ACCESS_KEY_ENV = "OSS_SECRET_ACCESS_KEY"
_OSS_REGION_ENV = "OSS_REGION"
_SUPPORTED_STORAGE = frozenset({"minio", "oss", "s3"})


def resolve_juicefs_volume_name() -> str:
    """Volume name for ``juicefs format``; override via ``JUICEFS_VOLUME_NAME`` env."""

    custom = os.getenv(JUICEFS_VOLUME_NAME_ENV, "").strip()
    return custom or JUICEFS_VOLUME_NAME


def resolve_juicefs_data_bucket() -> str:
    custom = os.getenv(JUICEFS_DATA_BUCKET_ENV, "").strip()
    return custom or JUICEFS_DATA_BUCKET


def resolve_juicefs_storage() -> str:
    """Return JuiceFS object storage backend: ``minio``, ``oss``, or ``s3``."""

    explicit = os.getenv(JUICEFS_STORAGE_ENV, "").strip().lower()
    if explicit:
        if explicit not in _SUPPORTED_STORAGE:
            raise RuntimeError(
                f"unsupported {JUICEFS_STORAGE_ENV}={explicit!r}; "
                f"expected one of {sorted(_SUPPORTED_STORAGE)}"
            )
        return explicit
    if os.getenv(_OSS_ENDPOINT_ENV, "").strip():
        return "oss"
    bucket_url = os.getenv(JUICEFS_BUCKET_URL_ENV, "").strip().lower()
    if "oss-" in bucket_url or ".aliyuncs.com" in bucket_url:
        return "oss"
    return "minio"


def resolve_object_storage_credentials() -> tuple[str, str] | None:
    access_key = (
        os.getenv(_OSS_ACCESS_KEY_ID_ENV, "").strip()
        or os.getenv(_MINIO_ACCESS_KEY_ENV, "").strip()
    )
    secret_key = (
        os.getenv(_OSS_SECRET_ACCESS_KEY_ENV, "").strip()
        or os.getenv(_MINIO_SECRET_KEY_ENV, "").strip()
    )
    if not access_key or not secret_key:
        return None
    return access_key, secret_key


def _normalize_endpoint_host(endpoint: str) -> tuple[str, bool]:
    stripped = endpoint.strip()
    secure = stripped.startswith("https://")
    host = stripped.removeprefix("https://").removeprefix("http://").strip("/")
    return host, secure


def ensure_minio_bucket(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    secure: bool = False,
    region: str = "us-east-1",
) -> bool:
    """Create ``bucket`` if missing. Returns True when created, False if it existed."""

    host, endpoint_secure = _normalize_endpoint_host(endpoint)
    client = Minio(
        host,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure or endpoint_secure,
        region=region,
    )
    if client.bucket_exists(bucket):
        return False
    client.make_bucket(bucket)
    return True


def juicefs_meta_url(postgres: PostgresPersistenceConfig) -> str:
    """Postgres meta URL scoped to ``juicefs_schema`` for juicefs format/mount."""

    if (
        postgres.host is None
        or postgres.port is None
        or postgres.user is None
        or postgres.password is None
        or postgres.database is None
    ):
        raise RuntimeError("persistence.postgres connection fields incomplete for JuiceFS meta")
    user = quote_plus(postgres.user)
    password = quote_plus(postgres.password)
    schema = quote_plus(postgres.juicefs_schema)
    # JuiceFS only accepts the ``postgres://`` scheme (not ``postgresql://``).
    return (
        f"postgres://{user}:{password}@{postgres.host}:{postgres.port}/"
        f"{postgres.database}?sslmode=disable&search_path={schema}"
    )


def juicefs_minio_bucket_url(endpoint: str, *, bucket: str = JUICEFS_DATA_BUCKET) -> str:
    """Path-style MinIO bucket URL accepted by ``juicefs format --bucket``."""

    stripped = endpoint.strip()
    if "://" not in stripped:
        stripped = f"http://{stripped}"
    parsed = urlparse(stripped)
    if not parsed.hostname:
        raise ValueError(f"invalid MinIO endpoint: {endpoint}")
    netloc = parsed.netloc or parsed.path
    scheme = parsed.scheme or "http"
    return f"{scheme}://{netloc.rstrip('/')}/{bucket}"


def juicefs_oss_bucket_url(
    endpoint: str,
    *,
    bucket: str | None = None,
    secure: bool = True,
) -> str:
    """Virtual-hosted OSS bucket URL accepted by ``juicefs format --storage oss``."""

    bucket_name = bucket or resolve_juicefs_data_bucket()
    host, endpoint_secure = _normalize_endpoint_host(endpoint)
    scheme = "https" if secure or endpoint_secure else "http"
    return f"{scheme}://{bucket_name}.{host}"


def juicefs_object_storage_bucket_url(
    storage: str | None = None,
    *,
    bucket: str | None = None,
) -> str:
    """Resolve the bucket URL for the configured object storage backend."""

    bucket_name = bucket or resolve_juicefs_data_bucket()
    override = os.getenv(JUICEFS_BUCKET_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")

    backend = storage or resolve_juicefs_storage()
    if backend == "oss":
        endpoint = os.getenv(_OSS_ENDPOINT_ENV, "").strip()
        if not endpoint:
            raise RuntimeError(
                f"{_OSS_ENDPOINT_ENV} or {JUICEFS_BUCKET_URL_ENV} is required when "
                f"{JUICEFS_STORAGE_ENV}=oss"
            )
        return juicefs_oss_bucket_url(endpoint, bucket=bucket_name)

    endpoint = os.getenv(_MINIO_ENDPOINT_ENV, "").strip()
    if not endpoint:
        raise RuntimeError(
            f"{_MINIO_ENDPOINT_ENV} or {JUICEFS_BUCKET_URL_ENV} is required when "
            f"{JUICEFS_STORAGE_ENV}=minio"
        )
    return juicefs_minio_bucket_url(endpoint, bucket=bucket_name)


def object_storage_env_ready() -> bool:
    credentials = resolve_object_storage_credentials()
    if credentials is None:
        return False
    if os.getenv(JUICEFS_BUCKET_URL_ENV, "").strip():
        return True
    storage = resolve_juicefs_storage()
    if storage == "oss":
        return bool(os.getenv(_OSS_ENDPOINT_ENV, "").strip())
    return bool(os.getenv(_MINIO_ENDPOINT_ENV, "").strip())


def minio_env_ready() -> bool:
    """Backward-compatible alias for legacy MinIO-only checks."""

    return object_storage_env_ready()


def postgres_connection_complete(postgres: PostgresPersistenceConfig | None) -> bool:
    if postgres is None:
        return False
    return (
        postgres.host is not None
        and postgres.port is not None
        and postgres.user is not None
        and postgres.password is not None
        and postgres.database is not None
    )


def ensure_juicefs_object_storage_bucket() -> str | None:
    """Ensure the JuiceFS data bucket exists when the backend supports auto-create.

    Returns the bucket name when configured, or ``None`` when object storage env
    is incomplete. OSS buckets must be created out-of-band.
    """

    credentials = resolve_object_storage_credentials()
    if credentials is None:
        logger.info(
            "skip JuiceFS object storage bucket bootstrap: set OSS_* or MINIO_* credentials"
        )
        return None

    bucket = resolve_juicefs_data_bucket()
    storage = resolve_juicefs_storage()
    if storage == "oss":
        logger.info(
            "JuiceFS OSS bucket {} must already exist; skipping auto-create",
            bucket,
        )
        return bucket

    endpoint = os.getenv(_MINIO_ENDPOINT_ENV, "").strip()
    if not endpoint:
        logger.info(
            "skip JuiceFS MinIO bucket bootstrap: set {} to create {}",
            _MINIO_ENDPOINT_ENV,
            bucket,
        )
        return None

    access_key, secret_key = credentials
    created = ensure_minio_bucket(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
    )
    if created:
        logger.info("created JuiceFS MinIO bucket: {}", bucket)
    else:
        logger.info("JuiceFS MinIO bucket already present: {}", bucket)
    return bucket


def ensure_juicefs_minio_bucket() -> str | None:
    """Backward-compatible alias."""

    return ensure_juicefs_object_storage_bucket()
