"""Integration: deepagents_sandbox restore → execute → capture (requires bwrap on Linux)."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
import unittest

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow_platform.application.agent.knowledge_assistant.bubblewrap_workspace import (
    capture_bubblewrap_workspace,
    restore_bubblewrap_workspace,
    with_artifacts_root,
)
from viknow_platform.application.agent.knowledge_assistant.bubblewrap_workspace import prepare_host_staging
from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agent.knowledge_assistant.sandbox_runtime import create_bubblewrap_sandbox
from viknow_platform.application.agent.knowledge_assistant.workspace_config import session_workspace_dir


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("bwrap"), "requires Linux bubblewrap")
class BubblewrapSandboxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restore_execute_capture_roundtrip(self) -> None:
        session_id = "bwrap-integration"
        workspace_dir = prepare_host_staging(session_id, self._profile.enabled_skills)
        runtime = create_bubblewrap_sandbox()
        try:
            restore_bubblewrap_workspace(runtime.backend, workspace_dir)
            backend = with_artifacts_root(runtime.backend)
            result = backend.execute("mkdir -p /workspace/scratch && echo roundtrip > /workspace/scratch/marker.txt")
            self.assertEqual(result.exit_code, 0, msg=result.output)
            capture_bubblewrap_workspace(runtime.backend, workspace_dir, session_id)
        finally:
            if runtime.close:
                runtime.close()
        marker = session_workspace_dir(session_id) / "scratch" / "marker.txt"
        self.assertTrue(marker.is_file())
        self.assertIn("roundtrip", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
