"""Pinned skill text in deepagents system prompt."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agent.knowledge_assistant.skill_pin import pinned_skills_prompt
from viknow_platform.application.agent.knowledge_assistant.workspace_lifecycle import workspace_runtime_instruction
from viknow_platform.application.agent.knowledge_assistant.session_workspace import ensure_session_workspace


class SkillPinTests(unittest.TestCase):
    def test_pinned_includes_knowledge_qa(self) -> None:
        profile = load_viknow_v2_profile()
        text = pinned_skills_prompt(profile.enabled_skills)
        self.assertIn("knowledge-qa-v2", text)
        self.assertIn("Non-Negotiables", text)

    def test_runtime_instruction_without_library(self) -> None:
        root = ensure_session_workspace("skill-pin-no-lib")
        from viknow_platform.application.agent.knowledge_assistant.workspace_lifecycle import SessionWorkspaceContext

        ctx = SessionWorkspaceContext(session_id="skill-pin-no-lib", root=root, upload_virtual_paths=())
        overlay = workspace_runtime_instruction(ctx, [], "deepagents")
        self.assertIn("no_library_granted", overlay)
        self.assertIn(".skills/", overlay)


if __name__ == "__main__":
    unittest.main()
