"""Bubblewrap restore/capture with sandbox upload/download (viknow2 parity)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow3.application.agent.qa_box.bubblewrap_workspace import (
    capture_bubblewrap_workspace,
    prepare_host_staging,
    restore_bubblewrap_workspace,
)
from viknow3.application.agent.qa_box.profile import load_viknow_v2_profile


class _MemorySandbox:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def upload_files(self, files: list[tuple[str, bytes]]):
        out = []
        for path, content in files:
            self._files[path] = content
            out.append(SimpleNamespace(path=path, error=None))
        return out

    def download_files(self, paths: list[str]):
        return [
            SimpleNamespace(path=path, content=self._files.get(path), error=None if path in self._files else "missing")
            for path in paths
        ]

    def glob(self, pattern: str, path: str | None = None):
        prefix = (path or "/workspace").rstrip("/") + "/"
        matches = [{"path": key, "is_dir": False} for key in sorted(self._files) if key.startswith(prefix)]
        return SimpleNamespace(matches=matches, error=None)


class BubblewrapWorkspaceSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restore_uploads_skills_into_sandbox(self) -> None:
        session_id = "restore-session"
        workspace_dir = prepare_host_staging(session_id, self._profile.enabled_skills)
        sandbox = _MemorySandbox()
        restore_bubblewrap_workspace(sandbox, workspace_dir)
        self.assertTrue(any("/workspace/.skills/knowledge-qa-v2/SKILL.md" in p for p in sandbox._files))

    def test_capture_merges_outputs_to_host(self) -> None:
        session_id = "capture-session"
        workspace_dir = prepare_host_staging(session_id, self._profile.enabled_skills)
        sandbox = _MemorySandbox()
        sandbox._files["/workspace/scratch/marker.txt"] = b"ok"
        capture_bubblewrap_workspace(sandbox, workspace_dir, session_id)
        self.assertEqual((workspace_dir / "scratch" / "marker.txt").read_text(encoding="utf-8"), "ok")
        self.assertFalse((workspace_dir / ".skills").exists())


if __name__ == "__main__":
    unittest.main()
