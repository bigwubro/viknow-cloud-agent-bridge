"""Bubblewrap restore/capture via deepagents_sandbox upload/download (viknow2 parity)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deepagents.backends.composite import CompositeBackend

from viknow_platform.application.agent.knowledge_assistant.workspace_lifecycle import prepare_session_run
from viknow_platform.application.agent.knowledge_assistant.workspace_config import (
    load_deepagents_workspace_config,
    session_workspace_dir,
)
from viknow_platform.application.agent.knowledge_assistant.workspace_staging import (
    _CAPTURE_WORKSPACE_PREFIXES,
    _RESTORE_WORKSPACE_PREFIXES,
    _RUNTIME_SKILLS_DIR,
    batch,
    list_workspace_paths,
    merge_host_workspace,
    prepare_bubblewrap_workspace_access,
    runtime_skill_uploads,
    workspace_uploads,
)

log = logging.getLogger(__name__)

_UPLOAD_BATCH = 25


def prepare_host_staging(session_id: str, enabled_skills: Sequence[str]) -> Path:
    return prepare_session_run(session_id, enabled_skills).root


def with_artifacts_root(backend: Any, artifacts_root: str = "/workspace") -> Any:
    return CompositeBackend(default=backend, routes={}, artifacts_root=artifacts_root)


def restore_bubblewrap_workspace(backend: Any, workspace_dir: Path) -> None:
    uploads = workspace_uploads(workspace_dir, prefixes=_RESTORE_WORKSPACE_PREFIXES) + runtime_skill_uploads(
        workspace_dir
    )
    if not uploads:
        return
    for chunk in batch(uploads, _UPLOAD_BATCH):
        responses = backend.upload_files(chunk)
        errors = [response for response in responses if getattr(response, "error", None)]
        if errors:
            first = errors[0]
            raise RuntimeError(f"failed to restore workspace file {first.path}: {first.error}")
        prepare_bubblewrap_workspace_access(backend, [path for path, _content in chunk])


def capture_bubblewrap_workspace(backend: Any, workspace_dir: Path, session_id: str = "") -> None:
    cfg = load_deepagents_workspace_config()
    paths = list_workspace_paths(backend, prefixes=_CAPTURE_WORKSPACE_PREFIXES)
    files: list[tuple[str, bytes]] = []
    max_bytes = cfg.max_file_size_mb * 1024 * 1024
    for chunk in batch(paths, _UPLOAD_BATCH):
        responses = backend.download_files(chunk)
        for response in responses:
            if getattr(response, "error", None) or getattr(response, "content", None) is None:
                continue
            raw_content = response.content
            content = raw_content if isinstance(raw_content, bytes) else str(raw_content).encode("utf-8")
            if len(content) > max_bytes:
                continue
            files.append((str(response.path), content))
    merge_host_workspace(workspace_dir, files, prefixes=_CAPTURE_WORKSPACE_PREFIXES)
    skills_dir = workspace_dir / _RUNTIME_SKILLS_DIR
    if skills_dir.exists():
        shutil.rmtree(skills_dir)
