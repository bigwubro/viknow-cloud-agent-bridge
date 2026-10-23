"""FilesystemManager: session file lifecycle on the shared session path tree."""

from __future__ import annotations

import logging
import mimetypes
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from viknow.core.config import WorkspaceConfig
from viknow.core.filesystem.paths import (
    OUTPUTS_DIR,
    SCRATCH_DIR,
    UPLOADS_DIR,
    WORKSPACE_VIRTUAL_ROOT,
    file_extension,
    safe_thread_id,
    sanitize_file_name,
    staging_relative_from_object_key,
    storage_relative_for_output,
    storage_relative_for_upload,
    thread_id_for_session,
    virtual_path_for_upload,
)
from viknow.core.session.domain import (
    SessionFileKind,
    SessionFileNotFound,
    SessionFileRecord,
    SessionFileStatus,
    SessionFileTooLarge,
    SessionNotFound,
)
from viknow.core.session.mapping import now
from viknow.core.session.store import SessionStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadPromptEntry:
    """Attachment entry injected into the agent prompt."""

    file_name: str
    virtual_path: str
    file_id: str


class FilesystemManager:
    """Owns session file upload/download and prepare/finalize for runs.

    ``workspace.root_dir`` (``{VIKNOW_FILES_ROOT}/sessions`` in shared deployments)
    is the single source of truth: uploads, captured outputs and scratch all live
    under ``{root_dir}/{safe_thread}/`` and metadata stores the workspace-relative
    storage path. There is no blob store behind it.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        workspace_config: WorkspaceConfig,
    ) -> None:
        self.session_store = session_store
        self.config = workspace_config
        if self.config.root_dir is None:
            raise ValueError("workspace.root_dir is required for FilesystemManager")

    @property
    def staging_root(self) -> Path:
        assert self.config.root_dir is not None
        return self.config.root_dir

    def staging_dir_for(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
    ) -> Path:
        thread = thread_id_for_session(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        return self.staging_root / safe_thread_id(thread)

    def upload_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_name: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> SessionFileRecord:
        session = self.session_store.get_session_for_user(user_id=user_id, session_id=session_id)
        if session is None:
            raise SessionNotFound(session_id)

        max_bytes = self.config.upload.max_file_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise SessionFileTooLarge(max_file_mb=self.config.upload.max_file_mb)

        # Wide-open upload: empty allowed_extensions means any type.
        allowed = {ext.lower().lstrip(".") for ext in self.config.upload.allowed_extensions}
        safe_name = sanitize_file_name(file_name)
        if allowed:
            ext = file_extension(safe_name)
            if ext not in allowed:
                raise ValueError(f"file type not allowed: .{ext or 'unknown'}")

        file_id = uuid4().hex
        storage_relative = storage_relative_for_upload(file_id=file_id, file_name=safe_name)
        virtual_path = virtual_path_for_upload(file_id=file_id, file_name=safe_name)
        guessed_mime = mime_type or mimetypes.guess_type(safe_name)[0]

        staging = self.staging_dir_for(
            user_id=user_id,
            workspace_id=session.workspace_id,
            session_id=session_id,
        )
        self._ensure_workspace_dirs(staging)
        staging_file = staging / storage_relative
        staging_file.write_bytes(content)

        record = SessionFileRecord(
            id=file_id,
            session_id=session_id,
            user_id=user_id,
            workspace_id=session.workspace_id,
            file_name=safe_name,
            mime_type=guessed_mime,
            size_bytes=len(content),
            kind=SessionFileKind.UPLOAD,
            status=SessionFileStatus.UPLOADED,
            object_key=storage_relative,
            virtual_path=virtual_path,
            uploaded_at=now(),
        )
        self.session_store.add_session_file(record)
        self._maybe_parse_upload(record, content)
        refreshed = self.session_store.get_session_file(file_id)
        return refreshed if refreshed is not None else record

    def list_session_files(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[SessionFileRecord]:
        session = self.session_store.get_session_for_user(user_id=user_id, session_id=session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return self.session_store.list_session_files(user_id=user_id, session_id=session_id)

    def get_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> SessionFileRecord:
        record = self.session_store.get_session_file_for_user(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )
        if record is None:
            raise SessionFileNotFound(file_id)
        return record

    def user_message_attachments(
        self,
        *,
        user_id: str,
        session_id: str,
        file_ids: list[str] | None,
    ) -> list[dict[str, object]]:
        """Build USER_MESSAGE event attachment payloads for timeline restore."""
        if not file_ids:
            return []
        items: list[dict[str, object]] = []
        for file_id in file_ids:
            try:
                record = self.get_session_file(
                    user_id=user_id,
                    session_id=session_id,
                    file_id=file_id,
                )
            except SessionFileNotFound:
                items.append(
                    {
                        "file_id": file_id,
                        "file_name": file_id,
                        "size_bytes": 0,
                    }
                )
                continue
            items.append(
                {
                    "file_id": record.id,
                    "file_name": record.file_name,
                    "size_bytes": record.size_bytes,
                }
            )
        return items

    def open_session_file_stream(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> tuple[SessionFileRecord, Iterator[bytes]]:
        record = self.get_session_file(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )
        stored = self.storage_path_for(
            user_id=user_id,
            workspace_id=record.workspace_id,
            session_id=session_id,
            record=record,
        )
        if not stored.is_file():
            raise SessionFileNotFound(file_id)

        def _iter_stored() -> Iterator[bytes]:
            yield stored.read_bytes()

        return record, _iter_stored()

    def delete_session_file(
        self,
        *,
        user_id: str,
        session_id: str,
        file_id: str,
    ) -> None:
        record = self.get_session_file(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        )
        # Metadata first so list/download never point at a file that is about to vanish.
        if not self.session_store.soft_delete_session_file(
            user_id=user_id,
            session_id=session_id,
            file_id=file_id,
        ):
            raise SessionFileNotFound(file_id)

        stored = self.storage_path_for(
            user_id=user_id,
            workspace_id=record.workspace_id,
            session_id=session_id,
            record=record,
        )
        try:
            if stored.is_file():
                stored.unlink()
        except OSError:
            logger.exception(
                "failed to delete session file after soft-delete file_id=%s path=%s",
                file_id,
                stored,
            )
            raise

    def prepare_run_workspace(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        attachment_file_ids: list[str] | None = None,
    ) -> list[UploadPromptEntry]:
        staging = self.staging_dir_for(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        self._ensure_workspace_dirs(staging)

        records = self.session_store.list_session_files(user_id=user_id, session_id=session_id)
        uploads = [
            record
            for record in records
            if record.kind == SessionFileKind.UPLOAD
            and record.status in {SessionFileStatus.UPLOADED, SessionFileStatus.PARSED}
        ]
        if attachment_file_ids is not None and len(attachment_file_ids) > 0:
            wanted = set(attachment_file_ids)
            uploads = [record for record in uploads if record.id in wanted]
            missing = wanted - {record.id for record in uploads}
            if missing:
                from viknow.core.session.domain import InvalidAttachmentFiles

                raise InvalidAttachmentFiles(sorted(missing))

        entries: list[UploadPromptEntry] = []
        for record in uploads:
            target = self.storage_path_for(
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                record=record,
            )
            if not target.is_file():
                raise SessionFileNotFound(record.id)
            entries.append(
                UploadPromptEntry(
                    file_name=record.file_name,
                    virtual_path=record.virtual_path,
                    file_id=record.id,
                )
            )
        return entries

    def resolve_workspace_upload_path(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        workspace_path: str,
    ) -> Path:
        virtual_path = _normalize_workspace_upload_path(workspace_path)
        if virtual_path is None:
            raise SessionFileNotFound(str(workspace_path or ""))

        records = self.session_store.list_session_files(user_id=user_id, session_id=session_id)
        record = next(
            (
                item
                for item in records
                if item.kind == SessionFileKind.UPLOAD
                and item.status in {SessionFileStatus.UPLOADED, SessionFileStatus.PARSED}
                and _normalize_workspace_upload_path(item.virtual_path) == virtual_path
            ),
            None,
        )
        if record is None:
            raise SessionFileNotFound(workspace_path)

        target = self.storage_path_for(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            record=record,
        )
        if not target.is_file():
            raise SessionFileNotFound(record.id)
        return target

    def storage_path_for(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        record: SessionFileRecord,
    ) -> Path:
        """Absolute path of a registered session file on the shared session tree."""

        thread = thread_id_for_session(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        relative = staging_relative_from_object_key(
            prefix=self.config.prefix,
            safe_thread=safe_thread_id(thread),
            object_key=record.object_key,
        )
        return self.staging_root / safe_thread_id(thread) / relative

    def finalize_run_workspace(
        self,
        *,
        user_id: str,
        workspace_id: str,
        session_id: str,
        captured_files: list[tuple[str, bytes]],
    ) -> None:
        """Run 结束后落盘：把 capture 的 outputs/scratch 写回会话路径树。

        ``captured_files`` 来自 bubblewrap/filesystem capture（相对 ``/workspace`` 的路径 + 字节）。
        只处理 ``outputs/`` 与 ``scratch/``：写入会话树后按 staging 配置清理本地 outputs/scratch；
        ``uploads/`` 必须保留且不得被本函数改动。
        """

        staging = self.staging_dir_for(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        uploads_before = _dir_fingerprint(staging / UPLOADS_DIR)

        for relative, content in captured_files:
            rel = relative.lstrip("/")
            if not (rel.startswith(f"{OUTPUTS_DIR}/") or rel.startswith(f"{SCRATCH_DIR}/")):
                continue
            target = staging / storage_relative_for_output(relative_path=rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        if self.config.staging.purge_outputs_after_sync:
            outputs = staging / OUTPUTS_DIR
            if outputs.exists():
                shutil.rmtree(outputs)
            outputs.mkdir(parents=True, exist_ok=True)
        if self.config.staging.purge_scratch_after_run:
            scratch = staging / SCRATCH_DIR
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch.mkdir(parents=True, exist_ok=True)

        if self.config.staging.keep_uploads_cache:
            uploads_after = _dir_fingerprint(staging / UPLOADS_DIR)
            if uploads_before != uploads_after:
                raise RuntimeError("finalize must not modify staging uploads cache")

    def _ensure_workspace_dirs(self, staging: Path) -> None:
        for name in (UPLOADS_DIR, OUTPUTS_DIR, SCRATCH_DIR):
            (staging / name).mkdir(parents=True, exist_ok=True)

    def _maybe_parse_upload(self, record: SessionFileRecord, content: bytes) -> None:
        """P1 gate: only parse when enabled and extension is allowlisted."""

        del content
        if not self.config.parse.enabled:
            return
        ext = file_extension(record.file_name)
        allowed = {item.lower().lstrip(".") for item in self.config.parse.allowed_extensions}
        if ext not in allowed:
            return
        # Parser not wired in P0; keep original blob and uploaded status.
        logger.info(
            "parse.enabled for %s but no parser registered; keeping status=uploaded",
            record.file_name,
        )


def _dir_fingerprint(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    result: dict[str, tuple[int, int]] = {}
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        stat = file_path.stat()
        result[file_path.relative_to(path).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return result


def format_uploads_prompt(entries: list[UploadPromptEntry]) -> str:
    if not entries:
        return ""
    lines = [
        "The following user-uploaded files are available in the workspace "
        "(paths only; contents are not inlined):",
        "Current capability note: there is no dedicated parser for complex formats "
        "(e.g. RTF/DOCX/PDF/Office/media). Prefer conservative handling:",
        "- Treat the list as file presence + path hints, not as extracted text.",
        "- You may try read_file / execute for plain text or very simple inspection only.",
        "- Do not invent, paraphrase, or guess file contents you could not reliably read.",
        "- If a file cannot be read clearly, say so briefly and ask the user what matters "
        "or to paste the relevant excerpt.",
        "Uploaded files:",
    ]
    for entry in entries:
        lines.append(f"- {entry.file_name}: {entry.virtual_path}")
    return "\n".join(lines)


def _normalize_workspace_upload_path(workspace_path: str) -> str | None:
    raw = str(workspace_path or "").strip()
    if not raw:
        return None
    if raw.startswith(f"{WORKSPACE_VIRTUAL_ROOT}/"):
        raw = raw[len(WORKSPACE_VIRTUAL_ROOT) + 1 :]
    rel = PurePosixPath(raw.lstrip("/"))
    parts = rel.parts
    if len(parts) < 2 or parts[0] != UPLOADS_DIR or any(part in {"", ".", ".."} for part in parts):
        return None
    return rel.as_posix()
