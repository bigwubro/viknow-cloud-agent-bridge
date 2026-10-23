"""Per-session agent workspace: prepare / finalize for all runtimes (production contract)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from viknow3.application.agent.knowledge_assistant.session_workspace import (
    ensure_session_workspace,
    materialize_session_uploads,
    purge_session_outputs,
    purge_session_scratch,
    stage_runtime_skills,
    workspace_upload_hint,
)
from viknow3.application.agent.knowledge_assistant.workspace_config import (
    load_deepagents_workspace_config,
    session_workspace_dir,
)
from viknow3.clock import new_id, now
from viknow3.store import STORE, FileRec

log = logging.getLogger(__name__)

_VIRTUAL_ROOT = "/workspace"
_RUNTIME_SKILLS_DIR = ".skills"
_CAPTURE_PREFIXES = ("outputs", "scratch")
_WORKSPACE_ARTIFACT_KIND = "workspace_run_artifact"


@dataclass(frozen=True)
class SessionWorkspaceContext:
    """Host staging directory for one session (maps to virtual `/workspace/` in prompts)."""

    session_id: str
    root: Path
    upload_virtual_paths: tuple[str, ...]

    @property
    def skills_dir(self) -> Path:
        return self.root / _RUNTIME_SKILLS_DIR


def prepare_session_run(session_id: str, enabled_skills: Sequence[str]) -> SessionWorkspaceContext:
    """Before any agent turn: dirs, staged skills, uploads, restore prior run artifacts."""
    root = ensure_session_workspace(session_id)
    stage_runtime_skills(session_id, enabled_skills)
    uploads = tuple(materialize_session_uploads(session_id))
    _restore_persisted_artifacts(session_id, root)
    return SessionWorkspaceContext(session_id=session_id, root=root, upload_virtual_paths=uploads)


def finalize_session_run(session_id: str) -> int:
    """After a turn: persist outputs/scratch, drop ephemeral `.skills/`, optional staging purge."""
    root = session_workspace_dir(session_id)
    if not root.is_dir():
        return 0
    written = _capture_host_artifacts(session_id, root)
    skills_dir = root / _RUNTIME_SKILLS_DIR
    if skills_dir.is_dir():
        shutil.rmtree(skills_dir, ignore_errors=True)
    purge_session_scratch(session_id)
    purge_session_outputs(session_id)
    return written


def workspace_runtime_instruction(
    ctx: SessionWorkspaceContext,
    library_ids: Sequence[str],
    runtime: str,
) -> str:
    """Runtime overlay: uploads, authorized libraries, KB-first tool policy."""
    parts: list[str] = [workspace_upload_hint(ctx.session_id)]
    libs = [str(item).strip() for item in library_ids if str(item).strip()]
    if libs:
        joined = ", ".join(libs)
        parts.append(
            f"\n本轮授权知识库 library_id：{joined}。"
            " 知识库相关问题必须先调用 knowledge_discover / knowledge_search（及 skill 要求的 inspect 工具）获取证据，"
            "禁止用 glob、grep、ls 在 workspace 里猜测答案。\n"
        )
        if runtime == "deepagents":
            parts.append(
                "匹配知识库问答时，第一个工具调用必须是 read_file 读取 "
                f"`{_VIRTUAL_ROOT}/.skills/knowledge-qa-v2/SKILL.md`（limit=1000），再按 skill 执行。\n"
            )
        elif runtime == "adk":
            parts.append(
                "匹配知识库问答时，必须先 load_skill(\"knowledge-qa-v2\")，再按 skill 调用检索工具。\n"
            )
    parts.append(
        f"\n会话工作区（虚拟路径前缀 `{_VIRTUAL_ROOT}/`）："
        f"uploads/ 用户上传，scratch/ 临时计算，outputs/ 可交付产物；"
        f"宿主机目录 `{ctx.root}`。\n"
    )
    return "".join(parts)


def _collect_prefix_files(root: Path, prefixes: Sequence[str]) -> list[tuple[str, bytes]]:
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
                log.warning("skip workspace capture %s: %s", relative, exc)
    return collected


def _capture_host_artifacts(session_id: str, root: Path) -> int:
    cfg = load_deepagents_workspace_config()
    max_bytes = cfg.max_file_size_mb * 1024 * 1024
    captured = _collect_prefix_files(root, _CAPTURE_PREFIXES)
    if not captured:
        return 0
    session = STORE.sessions.get(session_id)
    if session is None:
        return 0
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
                workspace_id=session.workspace_id,
                session_id=session_id,
                agent_id=session.agent_id,
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


def _restore_persisted_artifacts(session_id: str, root: Path) -> None:
    cfg = load_deepagents_workspace_config()
    max_bytes = cfg.max_file_size_mb * 1024 * 1024
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
