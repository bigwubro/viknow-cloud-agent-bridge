"""Path helpers for session workspace object keys and virtual paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

WORKSPACE_VIRTUAL_ROOT = "/workspace"
UPLOADS_DIR = "uploads"
OUTPUTS_DIR = "outputs"
SCRATCH_DIR = "scratch"
DERIVATIVES_DIR = ".derivatives"

_UNSAFE_NAME_RE = re.compile(r"[^\w.\-()+ ]+", re.UNICODE)


def safe_thread_id(thread_id: str) -> str:
    """Map logical thread_id to a host-safe directory name."""

    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
    return f"{thread_id.replace('/', '_').replace(':', '_')}-{digest}"


def thread_id_for_session(*, user_id: str, workspace_id: str, session_id: str) -> str:
    return f"{user_id}:{workspace_id}:{session_id}"


def sanitize_file_name(file_name: str | None, *, default: str = "attachment.bin") -> str:
    """Return a basename-safe file name without path traversal."""

    raw = Path(file_name or "").name.strip().strip(".")
    if not raw or raw in {".", ".."}:
        return default
    cleaned = _UNSAFE_NAME_RE.sub("_", raw).strip(" ._")
    return cleaned or default


def storage_file_name(*, file_id: str, file_name: str) -> str:
    return f"{file_id}_{sanitize_file_name(file_name)}"


def storage_relative_for_upload(*, file_id: str, file_name: str) -> str:
    """Storage path of an upload relative to the session workspace root."""

    name = storage_file_name(file_id=file_id, file_name=file_name)
    return f"{UPLOADS_DIR}/{name}"


def storage_relative_for_output(*, relative_path: str) -> str:
    """Storage path of a captured output/scratch file relative to the workspace root."""

    return _require_staging_relative(relative_path)


def virtual_path_for_upload(*, file_id: str, file_name: str) -> str:
    name = storage_file_name(file_id=file_id, file_name=file_name)
    return f"{WORKSPACE_VIRTUAL_ROOT}/{UPLOADS_DIR}/{name}"


def virtual_path_for_relative(relative_path: str) -> str:
    rel = PurePosixPath(relative_path.lstrip("/")).as_posix()
    return f"{WORKSPACE_VIRTUAL_ROOT}/{rel}"


def staging_relative_from_object_key(*, prefix: str, safe_thread: str, object_key: str) -> str:
    """Return the workspace-relative storage path for a registered session file.

    Records written since the files-root migration already store a workspace-relative
    path (``uploads/...``). Records written before it store the legacy MinIO object key
    ``{prefix}/{safe_thread}/...``; both resolve to the same path under the session tree.
    """

    key = str(object_key or "").lstrip("/")
    base = f"{prefix.strip('/')}/{safe_thread}/"
    if key.startswith(base):
        key = key[len(base) :]
    return _require_staging_relative(key)


def _require_staging_relative(relative_path: str) -> str:
    rel = PurePosixPath(str(relative_path or "").lstrip("/"))
    parts = rel.parts
    if (
        len(parts) < 2
        or parts[0] not in {UPLOADS_DIR, OUTPUTS_DIR, SCRATCH_DIR}
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"path does not match session workspace layout: {relative_path}")
    return rel.as_posix()


def file_extension(file_name: str) -> str:
    return Path(sanitize_file_name(file_name)).suffix.lower().lstrip(".")
