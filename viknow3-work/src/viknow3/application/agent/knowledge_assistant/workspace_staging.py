"""Host staging helpers for bubblewrap restore/capture (ported from viknow2 deepagents.py)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_WORKSPACE_ROOT = "/workspace"
_RUNTIME_SKILLS_DIR = ".skills"
_RESTORE_WORKSPACE_PREFIXES = ("uploads",)
_CAPTURE_WORKSPACE_PREFIXES = ("outputs", "scratch")
_EXCLUDED_WORKSPACE_DIRS = {".deepagents-staging", _RUNTIME_SKILLS_DIR}
_SANDBOX_UID = 1000
_SANDBOX_GID = 1000


def workspace_skills_source(backend: str) -> str:
    if backend == "bubblewrap":
        return f"{_WORKSPACE_ROOT}/{_RUNTIME_SKILLS_DIR}/"
    return "/workspace/.skills/"


def batch(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def workspace_uploads(
    workspace_dir: Path,
    *,
    prefixes: Sequence[str] | None = None,
) -> list[tuple[str, bytes]]:
    if not workspace_dir.exists():
        return []
    uploads: list[tuple[str, bytes]] = []
    roots = [workspace_dir / prefix for prefix in prefixes] if prefixes is not None else [workspace_dir]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or _is_excluded_workspace_path(path):
                continue
            relative = path.relative_to(workspace_dir).as_posix()
            uploads.append((f"{_WORKSPACE_ROOT}/{relative}", path.read_bytes()))
    return uploads


def runtime_skill_uploads(workspace_dir: Path) -> list[tuple[str, bytes]]:
    runtime_skill_root = workspace_dir / _RUNTIME_SKILLS_DIR
    if not runtime_skill_root.exists():
        return []
    uploads: list[tuple[str, bytes]] = []
    for path in sorted(runtime_skill_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace_dir).as_posix()
        uploads.append((f"{_WORKSPACE_ROOT}/{relative}", path.read_bytes()))
    return uploads


def list_workspace_paths(
    backend: Any,
    *,
    prefixes: Sequence[str] | None = None,
) -> list[str]:
    glob = getattr(backend, "glob", None)
    if not callable(glob):
        raise RuntimeError("sandbox backend does not support glob")
    search_roots = (
        [f"{_WORKSPACE_ROOT}/{prefix}" for prefix in prefixes]
        if prefixes is not None
        else [_WORKSPACE_ROOT]
    )
    paths: list[str] = []
    for root in search_roots:
        result = glob("**/*", path=root)
        if getattr(result, "error", None):
            continue
        for match in getattr(result, "matches", None) or []:
            if not isinstance(match, dict) or match.get("is_dir"):
                continue
            path = _workspace_match_path(match.get("path"))
            if path is None or _is_excluded_workspace_path(Path(path)):
                continue
            if prefixes is not None and not _path_has_prefix(path, prefixes):
                continue
            paths.append(path)
    return sorted(set(paths))


def merge_host_workspace(
    workspace_dir: Path,
    files: list[tuple[str, bytes]],
    *,
    prefixes: Sequence[str],
) -> None:
    import shutil

    for prefix in prefixes:
        target_root = workspace_dir / prefix
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
    for sandbox_path, content in files:
        relative = workspace_relative_path(sandbox_path)
        if relative is None or not relative.parts or relative.parts[0] not in prefixes:
            continue
        target = workspace_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def prepare_bubblewrap_workspace_access(backend: Any, paths: Sequence[str] | None = None) -> None:
    root = _native_workspace_root(backend)
    if root is None:
        return
    for target in _workspace_access_targets(root, paths):
        _make_accessible_to_sandbox_user(target)


def _workspace_match_path(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    if path.startswith("/"):
        return path
    return f"{_WORKSPACE_ROOT}/{path.lstrip('/')}"


def workspace_relative_path(sandbox_path: str) -> Path | None:
    if not sandbox_path.startswith(f"{_WORKSPACE_ROOT}/"):
        return None
    relative = PurePosixPath(sandbox_path.removeprefix(f"{_WORKSPACE_ROOT}/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return Path(*relative.parts)


def _path_has_prefix(sandbox_path: str, prefixes: Sequence[str]) -> bool:
    relative = workspace_relative_path(sandbox_path)
    if relative is None or not relative.parts:
        return False
    return relative.parts[0] in prefixes


def _is_excluded_workspace_path(path: Path) -> bool:
    return any(part in _EXCLUDED_WORKSPACE_DIRS for part in path.parts)


def _native_workspace_root(backend: Any) -> Path | None:
    native_sandbox = getattr(backend, "_sandbox", None)
    workspace = getattr(native_sandbox, "workspace", None)
    root = getattr(workspace, "root", None)
    if not isinstance(root, str) or not root:
        return None
    return Path(root).resolve()


def _workspace_access_targets(root: Path, paths: Sequence[str] | None) -> list[Path]:
    if paths is None:
        return [root]
    targets = {root}
    for sandbox_path in paths:
        relative = workspace_relative_path(sandbox_path)
        if relative is None:
            continue
        host_path = (root / relative).resolve()
        try:
            if os.path.commonpath([root, host_path]) != str(root):
                continue
        except ValueError:
            continue
        targets.add(host_path)
        for parent in host_path.parents:
            targets.add(parent)
            if parent == root:
                break
    return sorted(targets, key=lambda path: len(path.parts))


def _make_accessible_to_sandbox_user(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.geteuid() == 0:
            os.chown(path, _SANDBOX_UID, _SANDBOX_GID)
        required_mode = 0o777 if path.is_dir() else 0o666
        current_mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, current_mode | required_mode)
    except OSError as exc:
        raise RuntimeError(f"failed to prepare bubblewrap workspace permissions for {path}") from exc


__all__ = [
    "_CAPTURE_WORKSPACE_PREFIXES",
    "_RESTORE_WORKSPACE_PREFIXES",
    "_RUNTIME_SKILLS_DIR",
    "batch",
    "list_workspace_paths",
    "merge_host_workspace",
    "prepare_bubblewrap_workspace_access",
    "runtime_skill_uploads",
    "workspace_skills_source",
    "workspace_uploads",
]
