"""Session workspace prepare/finalize (runtime-agnostic)."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.workspace_lifecycle import (
    finalize_session_run,
    prepare_session_run,
    workspace_runtime_instruction,
)


class SessionWorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prepare_stages_skills_and_layout(self) -> None:
        ctx = prepare_session_run("lifecycle-session", self._profile.enabled_skills)
        self.assertTrue((ctx.root / "uploads").is_dir())
        self.assertTrue((ctx.root / "scratch").is_dir())
        self.assertTrue((ctx.skills_dir / "knowledge-qa-v2" / "SKILL.md").is_file())

    def test_finalize_captures_scratch(self) -> None:
        ctx = prepare_session_run("cap-session", self._profile.enabled_skills)
        marker = ctx.root / "scratch" / "out.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok", encoding="utf-8")
        finalize_session_run("cap-session")
        self.assertFalse((ctx.skills_dir).exists())

    def test_runtime_instruction_kb_policy(self) -> None:
        ctx = prepare_session_run("instr-session", ())
        text = workspace_runtime_instruction(ctx, ["lib-a"], "adk")
        self.assertIn("knowledge_search", text)
        self.assertIn("load_skill", text)
        self.assertIn("glob", text)


if __name__ == "__main__":
    unittest.main()
