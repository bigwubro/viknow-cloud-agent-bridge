"""Per-session workspace directories and upload materialization."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

from viknow3.application.agent.knowledge_assistant.workspace_config import (
    load_deepagents_workspace_config,
    session_workspace_dir,
)
from viknow3.store import STORE

log = logging.getLogger(__name__)

_SKILLS_PACKAGE_ROOT = Path(__file__).resolve().parent / "skills"
_STAGED_SKILLS_DIR = ".skills"
_WORKSPACE_SUBDIRS = ("scratch", "outputs", "uploads")


def ensure_session_workspace(session_id: str) -> Path:
    root = session_workspace_dir(session_id)
    for name in _WORKSPACE_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / _STAGED_SKILLS_DIR).mkdir(parents=True, exist_ok=True)
    return root


def stage_runtime_skills(session_id: str, enabled_skills: Sequence[str]) -> None:
    """Copy profile-enabled skills into the session workspace `.skills/` (viknow2 `_stage_runtime_skills`)."""
    root = ensure_session_workspace(session_id)
    staging = root / _STAGED_SKILLS_DIR
    for name in enabled_skills:
        skill_id = str(name).strip()
        if not skill_id:
            continue
        src = _SKILLS_PACKAGE_ROOT / skill_id
        if not src.is_dir():
            log.warning("skip staging missing skill %s (%s)", skill_id, src)
            continue
        dest = staging / skill_id
        shutil.copytree(src, dest, dirs_exist_ok=True)


def materialize_session_uploads(session_id: str) -> list[str]:
    """Copy access-store session files into workspace/uploads when content is available."""
    root = ensure_session_workspace(session_id)
    uploads_dir = root / "uploads"
    written: list[str] = []
    for record in STORE.files.values():
        if record.deleted or record.session_id != session_id:
            continue
        if not record.content:
            continue
        safe_name = Path(record.file_name).name or record.id
        target = uploads_dir / safe_name
        if target.exists() and target.stat().st_size == len(record.content):
            written.append(f"/workspace/uploads/{safe_name}")
            continue
        target.write_bytes(record.content)
        written.append(f"/workspace/uploads/{safe_name}")
    return written


def workspace_upload_hint(session_id: str) -> str:
    paths = materialize_session_uploads(session_id)
    if not paths:
        return ""
    lines = "\n".join(f"- `{path}`" for path in paths)
    return f"\n\n本轮会话已上传文件（workspace）：\n{lines}\n"


def purge_session_scratch(session_id: str) -> None:
    cfg = load_deepagents_workspace_config()
    if not cfg.staging_purge_scratch_after_run:
        return
    scratch = session_workspace_dir(session_id) / "scratch"
    if scratch.is_dir():
        for child in scratch.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


def purge_session_outputs(session_id: str) -> None:
    cfg = load_deepagents_workspace_config()
    if not cfg.staging_purge_outputs_after_sync:
        return
    outputs = session_workspace_dir(session_id) / "outputs"
    if outputs.is_dir():
        for child in outputs.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
