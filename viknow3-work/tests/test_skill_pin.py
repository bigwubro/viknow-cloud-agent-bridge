"""Pinned skill text in deepagents system prompt."""

from __future__ import annotations

import os
import unittest

import sys
from pathlib import Path

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from pg_test_env import apply as _apply_test_pg

_apply_test_pg()

from viknow3.application.agent.qa_box.profile import load_viknow_v2_profile
from viknow3.application.agent.qa_box.skill_pin import pinned_skills_prompt
from viknow3.application.agent.qa_box.session_workspace import ensure_session_workspace
from viknow3.application.agent.qa_box.workspace_lifecycle import (
    SessionWorkspaceContext,
    workspace_runtime_instruction,
)


class SkillPinTests(unittest.TestCase):
    def test_pinned_includes_knowledge_qa(self) -> None:
        profile = load_viknow_v2_profile()
        text = pinned_skills_prompt(profile.enabled_skills)
        self.assertIn("knowledge-qa-v2", text)
        self.assertIn("Non-Negotiables", text)

    def test_runtime_instruction_without_library(self) -> None:
        root = ensure_session_workspace("skill-pin-no-lib")
        ctx = SessionWorkspaceContext(session_id="skill-pin-no-lib", root=root, upload_virtual_paths=())
        overlay = workspace_runtime_instruction(ctx, [], "deepagents")
        self.assertIn("no_library_granted", overlay)
        self.assertIn(".skills/", overlay)


if __name__ == "__main__":
    unittest.main()
