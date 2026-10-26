"""Run-local workspace artifact sink for tools that return file manifests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


class _UploadResult(Protocol):
    error: str | None


class _DownloadResult(Protocol):
    content: bytes | None
    error: str | None


class _WorkspaceBackend(Protocol):
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[_UploadResult]: ...

    def download_files(self, paths: list[str]) -> list[_DownloadResult]: ...


@dataclass(frozen=True)
class _ArtifactContext:
    backend: _WorkspaceBackend
    root: str


_CURRENT_ARTIFACTS: ContextVar[_ArtifactContext | None] = ContextVar(
    "viknow_workspace_artifacts", default=None
)


@contextmanager
def bind_workspace_artifacts(backend: _WorkspaceBackend, *, root: str) -> Iterator[None]:
    token = _CURRENT_ARTIFACTS.set(_ArtifactContext(backend=backend, root=root.rstrip("/")))
    try:
        yield
    finally:
        _CURRENT_ARTIFACTS.reset(token)


def upload_workspace_artifacts(files: list[tuple[str, bytes]]) -> None:
    context = _CURRENT_ARTIFACTS.get()
    if context is None:
        raise RuntimeError("workspace artifact backend is not bound")
    absolute_files = [(f"{context.root}{path}", content) for path, content in files]
    results = context.backend.upload_files(absolute_files)
    if len(results) != len(absolute_files) or any(result.error for result in results):
        raise RuntimeError("workspace artifact upload failed")


def workspace_artifact_path(relative_path: str) -> str:
    context = _CURRENT_ARTIFACTS.get()
    if context is None:
        raise RuntimeError("workspace artifact backend is not bound")
    return f"{context.root}{relative_path}"


def download_workspace_artifact(relative_path: str, *, max_bytes: int) -> bytes:
    context = _CURRENT_ARTIFACTS.get()
    if context is None:
        raise RuntimeError("workspace artifact backend is not bound")
    if relative_path != "/scratch/run-analysis.json":
        raise ValueError("draft_path must be /scratch/run-analysis.json")
    responses = context.backend.download_files([f"{context.root}{relative_path}"])
    if len(responses) != 1 or responses[0].error or responses[0].content is None:
        raise ValueError("run analysis draft file could not be read")
    content = responses[0].content
    if len(content) > max_bytes:
        raise ValueError(f"run analysis draft exceeds {max_bytes} bytes")
    return content
