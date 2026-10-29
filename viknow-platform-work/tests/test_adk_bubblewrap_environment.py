"""ADK BubblewrapScriptEnvironment (integration, Linux + bwrap)."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
import unittest

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow_platform.application.agent.knowledge_assistant.adk_bubblewrap_environment import (
    BubblewrapScriptEnvironment,
    build_adk_script_environment,
)
from viknow_platform.application.agent.knowledge_assistant.workspace_config import DeepagentsSandboxConfig


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("bwrap"), "requires bubblewrap")
class AdkBubblewrapEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = __import__("pathlib").Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_execute_writes_under_workspace(self) -> None:
        cfg = DeepagentsSandboxConfig(timeout_seconds=30, max_output_bytes=262_144)
        env = BubblewrapScriptEnvironment(working_dir=self._root, sandbox_cfg=cfg)
        await env.initialize()
        try:
            result = await env.execute("mkdir -p scratch && echo sandbox-ok > scratch/marker.txt && cat scratch/marker.txt")
            self.assertEqual(result.exit_code, 0)
            self.assertIn("sandbox-ok", result.stdout)
            self.assertTrue((self._root / "scratch" / "marker.txt").is_file())
        finally:
            await env.close()

    async def test_factory_selects_bubblewrap(self) -> None:
        built = build_adk_script_environment(self._root, script_sandbox="bubblewrap", sandbox_cfg=DeepagentsSandboxConfig())
        self.assertIsInstance(built, BubblewrapScriptEnvironment)


if __name__ == "__main__":
    unittest.main()
