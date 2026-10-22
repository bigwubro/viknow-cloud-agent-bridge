"""Restore/capture for bubblewrap session workspace."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow3.application.agent.knowledge_assistant.bubblewrap_workspace import (
    _collect_workspace_files,
    capture_bubblewrap_workspace,
    restore_bubblewrap_workspace,
)
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.workspace_config import session_workspace_dir


class BubblewrapWorkspaceSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_collect_outputs_and_scratch(self) -> None:
        root = session_workspace_dir("collect-session")
        root.mkdir(parents=True, exist_ok=True)
        (root / "outputs").mkdir()
        (root / "scratch").mkdir()
        (root / "outputs" / "a.txt").write_text("out", encoding="utf-8")
        (root / "scratch" / "b.txt").write_text("tmp", encoding="utf-8")
        files = _collect_workspace_files(root, ("outputs", "scratch"))
        paths = {rel for rel, _ in files}
        self.assertEqual(paths, {"outputs/a.txt", "scratch/b.txt"})

    def test_capture_removes_runtime_skills_dir(self) -> None:
        session_id = "capture-skills-session"
        restore_bubblewrap_workspace(session_id, self._profile.enabled_skills)
        ws = session_workspace_dir(session_id)
        self.assertTrue((ws / ".skills" / "knowledge-qa-v2" / "SKILL.md").is_file())
        capture_bubblewrap_workspace(session_id)
        self.assertFalse((ws / ".skills").exists())


if __name__ == "__main__":
    unittest.main()
