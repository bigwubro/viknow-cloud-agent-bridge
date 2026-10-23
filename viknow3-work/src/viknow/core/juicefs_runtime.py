"""In-container (or host) JuiceFS format + mount.

Docker entrypoint runs ``python -m viknow.core.juicefs_runtime`` before uvicorn.
Requires the ``juicefs`` CLI (shipped in the base image) and FUSE privileges
(``privileged`` or ``SYS_ADMIN`` + ``/dev/fuse``).

Independent of ``VIKNOW_PERSISTENCE_BACKEND``: session may use sqlite while
JuiceFS still uses Postgres meta + object storage data.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from viknow.core.config import (
    FILES_ROOT_ENV,
    AppConfig,
    load_app_config,
    resolve_files_root,
    resolve_juicefs_meta_postgres,
)
from viknow.core.juicefs_bootstrap import (
    JUICEFS_BUCKET_URL_ENV,
    ensure_juicefs_object_storage_bucket,
    juicefs_meta_url,
    juicefs_object_storage_bucket_url,
    object_storage_env_ready,
    postgres_connection_complete,
    resolve_juicefs_storage,
    resolve_juicefs_volume_name,
    resolve_object_storage_credentials,
)
from viknow.core.postgres_bootstrap import ensure_juicefs_meta_schema

JUICEFS_ENABLED_ENV = "VIKNOW_JUICEFS_ENABLED"
_SENSITIVE_OPTION_NAMES = frozenset({"--access-key", "--secret-key", "--bucket"})
_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def juicefs_mount_enabled(config: AppConfig) -> bool:
    """Whether entrypoint should format/mount JuiceFS.

    ``VIKNOW_JUICEFS_ENABLED`` forces on/off. Unset = auto when object storage
    env, ``persistence.postgres`` connection fields, and ``juicefs`` CLI are
    present. Not tied to ``persistence.backend``.
    """

    explicit = os.getenv(JUICEFS_ENABLED_ENV, "").strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return False
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if shutil.which("juicefs") is None:
        return False
    if not object_storage_env_ready():
        return False
    try:
        meta_postgres = resolve_juicefs_meta_postgres(config)
    except RuntimeError:
        return False
    return postgres_connection_complete(meta_postgres)


def ensure_juicefs_volume(config: AppConfig, *, mount: bool = True) -> Path | None:
    """Create schema/bucket, format once, optionally mount at files_root.

    Returns the mountpoint when mount ran (or was already mounted), else None
    when skipped by policy.
    """

    if not juicefs_mount_enabled(config):
        logger.info(
            "skip JuiceFS format/mount (set {}=true to force; needs object storage "
            "credentials + JuiceFS Postgres connection + juicefs CLI)",
            JUICEFS_ENABLED_ENV,
        )
        return None

    if shutil.which("juicefs") is None:
        raise RuntimeError("juicefs CLI not found on PATH; install it in the image or on the host")
    postgres = resolve_juicefs_meta_postgres(config)
    if not postgres_connection_complete(postgres):
        raise RuntimeError(
            "JuiceFS Postgres connection fields are required for JuiceFS meta "
            "(set share.database.juicefs_postgres / JUICEFS_POSTGRES_* or "
            "persistence.postgres; independent of VIKNOW_PERSISTENCE_BACKEND)"
        )

    created_schema = ensure_juicefs_meta_schema(postgres)
    if created_schema:
        logger.info("created JuiceFS meta schema: {}", created_schema)

    if ensure_juicefs_object_storage_bucket() is None:
        raise RuntimeError(
            "Object storage credentials are required for JuiceFS data bucket bootstrap "
            "(set OSS_* or MINIO_ACCESS_KEY / MINIO_SECRET_KEY, plus OSS_ENDPOINT or "
            "MINIO_ENDPOINT / JUICEFS_BUCKET_URL)"
        )

    credentials = resolve_object_storage_credentials()
    if credentials is None:
        raise RuntimeError("Object storage credentials are required for JuiceFS format")
    access_key, secret_key = credentials
    storage = resolve_juicefs_storage()
    meta_url = juicefs_meta_url(postgres)
    bucket_url_override = os.getenv(JUICEFS_BUCKET_URL_ENV, "").strip()
    bucket_url = juicefs_object_storage_bucket_url(storage)

    _format_volume(
        meta_url=meta_url,
        bucket_url=bucket_url,
        access_key=access_key,
        secret_key=secret_key,
        storage=storage,
    )
    if not mount:
        return None

    env_root = os.getenv(FILES_ROOT_ENV, "").strip()
    mountpoint = Path(env_root) if env_root else resolve_files_root(config)
    mountpoint = mountpoint.resolve()
    _mount_volume(
        meta_url=meta_url,
        mountpoint=mountpoint,
        bucket_url_override=bucket_url_override or None,
    )
    return mountpoint


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    logger.info("{}", " ".join(_command_for_log(cmd)))
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _command_for_log(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for argument in cmd:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append("<redacted-url>" if "://" in argument else argument)
        redact_next = argument in _SENSITIVE_OPTION_NAMES
    return redacted


def _redact_sensitive_text(value: str, *secrets: str | None) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return _URL_PATTERN.sub("<redacted-url>", redacted)


def _volume_formatted(meta_url: str) -> bool:
    return _run(["juicefs", "status", meta_url], check=False).returncode == 0


def _is_mounted(mountpoint: Path) -> bool:
    result = subprocess.run(
        ["findmnt", "-n", str(mountpoint)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return True
    try:
        return mountpoint.is_mount()
    except OSError:
        return False


def _format_volume(
    *,
    meta_url: str,
    bucket_url: str,
    access_key: str,
    secret_key: str,
    storage: str,
) -> None:
    if _volume_formatted(meta_url):
        logger.info(
            "JuiceFS volume already formatted ({}); skip format", resolve_juicefs_volume_name()
        )
        return
    volume_name = resolve_juicefs_volume_name()
    result = _run(
        [
            "juicefs",
            "format",
            "--storage",
            storage,
            "--bucket",
            bucket_url,
            "--access-key",
            access_key,
            "--secret-key",
            secret_key,
            meta_url,
            volume_name,
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = _redact_sensitive_text(
            (result.stderr or result.stdout or "").strip(),
            access_key,
            secret_key,
            meta_url,
            bucket_url,
        )
        raise RuntimeError(f"juicefs format failed: {detail}")
    logger.info("formatted JuiceFS volume {} with storage {}", volume_name, storage)


def _mount_volume(
    *,
    meta_url: str,
    mountpoint: Path,
    bucket_url_override: str | None = None,
) -> None:
    mountpoint.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mountpoint):
        logger.info("already mounted: {}; skip mount", mountpoint)
        return
    cmd = ["juicefs", "mount", "-d"]
    if bucket_url_override:
        cmd.extend(["--bucket", bucket_url_override])
    cmd.extend([meta_url, str(mountpoint)])
    result = _run(cmd, check=False)
    if result.returncode != 0:
        detail = _redact_sensitive_text(
            (result.stderr or result.stdout or "").strip(),
            meta_url,
            bucket_url_override,
        )
        raise RuntimeError(
            f"juicefs mount failed at {mountpoint}: {detail}. "
            "Container needs privileged mode or CAP_SYS_ADMIN + /dev/fuse."
        )
    if not _is_mounted(mountpoint):
        raise RuntimeError(f"juicefs mount reported success but {mountpoint} is not a mountpoint")
    logger.info("mounted JuiceFS at {}", mountpoint)


def main() -> int:
    """Entrypoint for ``python -m viknow.core.juicefs_runtime``."""

    from viknow.core.env import load_local_env

    # The deployment may provide a container-local object-store endpoint that
    # must not be replaced by an image-local .env value.
    bucket_url_override = os.getenv(JUICEFS_BUCKET_URL_ENV)
    load_local_env()
    if bucket_url_override:
        os.environ[JUICEFS_BUCKET_URL_ENV] = bucket_url_override
    config = load_app_config()
    ensure_juicefs_volume(config, mount=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        detail = _redact_sensitive_text((exc.stderr or exc.stdout or str(exc)).strip())
        raise SystemExit(detail or str(exc)) from exc
