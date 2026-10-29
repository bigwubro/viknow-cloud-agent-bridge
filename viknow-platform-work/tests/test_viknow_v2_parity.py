"""viknow-v2 profile parity in viknow-platform (tools/skills surface)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from viknow_platform.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow_platform.application.agent.knowledge_assistant.tools import build_tools
from viknow_platform.application.agentcore.registry.catalog import Registry


class ViknowV2ParityTests(unittest.TestCase):
    def test_profile_matches_viknow2_tool_and_skill_lists(self) -> None:
        profile = load_viknow_v2_profile()
        self.assertEqual(profile.id, "viknow-v2")
        self.assertEqual(len(profile.enabled_tools), 10)
        self.assertIn("video_inspect_2_0", profile.enabled_tools)
        self.assertEqual(profile.enabled_skills, ("knowledge-qa-v2", "visual-ground"))
        self.assertIn("read_file", profile.system_prompt)

    def test_build_all_profile_tools(self) -> None:
        profile = load_viknow_v2_profile()
        tools = build_tools(profile)
        self.assertEqual({item.name for item in tools}, set(profile.enabled_tools))

    def test_registry_deepagents_exposes_full_tool_tuple(self) -> None:
        reg = Registry()
        entry = reg.profile("deepagents")
        assert entry is not None
        profile = load_viknow_v2_profile()
        self.assertEqual(entry.tools, profile.enabled_tools)


if __name__ == "__main__":
    unittest.main()
