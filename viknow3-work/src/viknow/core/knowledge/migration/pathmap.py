"""Logical-path mapping between environments and recursive JSON path rewriting.

A knowledge base stores ``file_path`` values in many places: plain text columns,
JSONB metadata, and Neo4j properties. The absolute prefix varies between
environments, but the relative structure ``knowledge/uploads/{library_id}/{asset_id}.ext``
(or ``knowledge/libraries/{workspace}/...`` for working-dir products) is stable.
This module converts arbitrary absolute paths to environment-agnostic logical
paths and back, and rewrites path-like keys inside nested JSON structures.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from viknow.core.config import (
    FILES_ROOT_KNOWLEDGE_DIR,
    FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR,
    FILES_ROOT_KNOWLEDGE_UPLOADS_DIR,
)

# Keys inside JSON structures whose string values are treated as file paths.
_PATH_KEY_RE = re.compile(
    r"(^|_)(path|uri|file|source_uri|source_path|video_path)(_|$)",
    re.IGNORECASE,
)

_UPLOADS_MARKER = f"{FILES_ROOT_KNOWLEDGE_DIR}/{FILES_ROOT_KNOWLEDGE_UPLOADS_DIR}/"
_LIBRARIES_MARKER = f"{FILES_ROOT_KNOWLEDGE_DIR}/{FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR}/"


def _normalize_parts(path: str) -> list[str] | None:
    """Return posix path parts under the stable knowledge subtree, or None."""
    normalized = str(path or "").replace("\\", "/")
    if not normalized:
        return None
    if _UPLOADS_MARKER in normalized:
        marker_index = normalized.find(_UPLOADS_MARKER)
        return PurePosixPath(normalized[marker_index:]).parts
    if _LIBRARIES_MARKER in normalized:
        marker_index = normalized.find(_LIBRARIES_MARKER)
        return PurePosixPath(normalized[marker_index:]).parts
    return None


def is_logical_path(path: str) -> bool:
    """True when ``path`` already starts with the stable knowledge subtree marker."""
    normalized = str(path or "").replace("\\", "/")
    return normalized.startswith(_UPLOADS_MARKER) or normalized.startswith(_LIBRARIES_MARKER)


def to_logical_path(path: str) -> str | None:
    """Convert an absolute ``file_path`` to its environment-agnostic logical form.

    The path is trimmed at the first ``knowledge/uploads/`` or
    ``knowledge/libraries/`` marker, so the leading absolute prefix (which varies
    between environments) is dropped. Returns ``None`` when the path does not
    belong to the knowledge subtree.
    """
    parts = _normalize_parts(path)
    if parts is None:
        return None
    return "/".join(parts)


def to_absolute_path(logical_path: str, *, files_root: str, library_id: str) -> str:
    """Resolve a logical path to an absolute path for the target environment.

    The ``{library_id}`` segment inside ``knowledge/uploads/`` (and the
    workspace segment inside ``knowledge/libraries/``) is replaced with the
    target ``library_id``, then joined under ``files_root``.
    """
    logical_path = str(logical_path or "").replace("\\", "/")
    if not logical_path:
        raise ValueError("logical_path must not be empty")
    parts = PurePosixPath(logical_path).parts
    if not parts:
        raise ValueError("logical_path must not be empty")
    if parts[0] != FILES_ROOT_KNOWLEDGE_DIR or len(parts) < 3:
        raise ValueError(f"invalid logical path: {logical_path}")

    kind = parts[1]
    if kind in (FILES_ROOT_KNOWLEDGE_UPLOADS_DIR, FILES_ROOT_KNOWLEDGE_LIBRARIES_DIR):
        rebuilt = [FILES_ROOT_KNOWLEDGE_DIR, kind, library_id, *parts[3:]]
    else:
        raise ValueError(f"invalid logical path kind: {logical_path}")

    root = str(files_root).rstrip("/")
    return f"{root}/{'/'.join(rebuilt)}"


def rewrite_json_paths(
    value: Any,
    *,
    rewrite: Callable[[str], str | None],
) -> Any:
    """Recursively rewrite path-like string values inside nested JSON structures.

    Keys matching ``_PATH_KEY_RE`` (e.g. ``file_path``, ``source_uri``) have
    their string values passed through ``rewrite``; a ``None`` return leaves the
    value untouched. Lists and dicts are traversed in place. All other values
    are returned unchanged.
    """
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if _PATH_KEY_RE.search(key) and isinstance(item, str):
                transformed = rewrite(item)
                rewritten[key] = transformed if transformed is not None else item
            else:
                rewritten[key] = rewrite_json_paths(item, rewrite=rewrite)
        return rewritten
    if isinstance(value, list):
        return [rewrite_json_paths(item, rewrite=rewrite) for item in value]
    return value


def collect_path_values(value: Any) -> set[str]:
    """Collect every path-like string value inside a JSON structure."""
    collected: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, sub in item.items():
                if _PATH_KEY_RE.search(key) and isinstance(sub, str):
                    collected.add(sub)
                visit(sub)
        elif isinstance(item, list):
            for sub in item:
                visit(sub)

    visit(value)
    return collected
