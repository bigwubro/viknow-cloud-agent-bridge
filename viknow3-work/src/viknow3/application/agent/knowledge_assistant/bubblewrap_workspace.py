"""Bubblewrap workspace restore/capture (viknow2 DeepAgentsRuntime parity)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

from viknow3.application.agent.knowledge_assistant.session_workspace import (
    ensure_session_workspace,
    materialize_session_uploads,
    purge_session_outputs,
    purge_session_scratch,
    stage_runtime_skills,
)
from viknow3.application.agent.knowledge_assistant.workspace_config import (
    load_deepagents_workspace_config,
    session_workspace_dir,
)
from viknow3.clock import new_id, now
from viknow3.store import STORE, FileRec

log = logging.getLogger(__name__)

_RUNTIME_SKILLS_DIR = ".skills"
_RESTORE_PREFIXES = ("uploads",)
_CAPTURE_PREFIXES = ("outputs", "scratch")
_WORKSPACE_ARTIFACT_KIND = "workspace_artifact"


def restore_bubblewrap_workspace(session_id: str, enabled_skills: Sequence[str]) -> None:
    """Before run: stage skills, materialize uploads, restore persisted workspace artifacts."""
    cfg = load_deepagents_workspace_config()
    root = ensure_session_workspace(session_id)
    stage_runtime_skills(session_id, enabled_skills)
    materialize_session_uploads(session_id)
    _restore_persisted_artifacts(session_id, root)


def capture_bubblewrap_workspace(session_id: str) -> int:
    """After run: pull outputs/scratch into access store, drop runtime `.skills/`, apply staging purge."""
    cfg = load_deepagents_workspace_config()
    root = session_workspace_dir(session_id)
    if not root.is_dir():
        return 0

    captured = _collect_workspace_files(root, _CAPTURE_PREFIXES)
    written = _persist_artifacts(session_id, captured)

    skills_dir = root / _RUNTIME_SKILLS_DIR
    if skills_dir.is_dir():
        shutil.rmtree(skills_dir, ignore_errors=True)

    if cfg.backend == "bubblewrap":
        purge_session_scratch(session_id)
        purge_session_outputs(session_id)
    return written


def _collect_workspace_files(root: Path, prefixes: Sequence[str]) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []
    for prefix in prefixes:
        base = root / prefix
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                collected.append((relative, path.read_bytes()))
            except OSError as exc:
                log.warning("skip capture %s: %s", relative, exc)
    return collected


def _restore_persisted_artifacts(session_id: str, root: Path) -> None:
    max_bytes = load_deepagents_workspace_config().max_file_size_mb * 1024 * 1024
    for record in STORE.files.values():
        if record.deleted or record.session_id != session_id or record.kind != _WORKSPACE_ARTIFACT_KIND:
            continue
        relative = record.file_name.strip().lstrip("/")
        if not relative or not any(relative.startswith(f"{prefix}/") for prefix in _CAPTURE_PREFIXES):
            continue
        if len(record.content) > max_bytes:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == len(record.content):
            continue
        target.write_bytes(record.content)


def _persist_artifacts(session_id: str, captured: list[tuple[str, bytes]]) -> int:
    if not captured:
        return 0
    session = STORE.sessions.get(session_id)
    if session is None:
        return 0
    workspace_id = session.workspace_id
    agent_id = session.agent_id
    max_bytes = load_deepagents_workspace_config().max_file_size_mb * 1024 * 1024
    moment = now()
    written = 0
    existing = {
        (rec.file_name, rec.session_id): rec
        for rec in STORE.files.values()
        if not rec.deleted and rec.session_id == session_id and rec.kind == _WORKSPACE_ARTIFACT_KIND
    }
    for relative, content in captured:
        if len(content) > max_bytes:
            continue
        key = (relative, session_id)
        record = existing.get(key)
        if record is not None and record.content == content:
            continue
        if record is not None:
            record.content = content
            record.size_bytes = len(content)
            record.updated_at = moment
            record._write()
        else:
            file_id = new_id()
            artifact = FileRec(
                id=file_id,
                workspace_id=workspace_id,
                session_id=session_id,
                agent_id=agent_id,
                file_name=relative,
                mime_type=None,
                size_bytes=len(content),
                kind=_WORKSPACE_ARTIFACT_KIND,
                status="ready",
                content=content,
                created_at=moment,
                updated_at=moment,
            )
            STORE.files[file_id] = artifact
            existing[key] = artifact
        written += 1
    return written
