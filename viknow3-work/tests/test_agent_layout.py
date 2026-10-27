"""Agent packages share one runtime and stay addressable by public id."""

from __future__ import annotations

import unittest
from pathlib import Path

from agents.layout import discover_agents, package_for_public_id


class AgentLayoutTests(unittest.TestCase):
    def test_public_ids(self) -> None:
        ids = [public for package in discover_agents() for public in package.public_ids()]
        self.assertEqual(
            ids,
            [
                "expertbuddy-adk",
                "expertbuddy-deepagents",
            ],
        )
        self.assertEqual(package_for_public_id("expertbuddy-deepagents").directory, "expert_buddy")
        self.assertEqual(package_for_public_id("expertbuddy-adk").directory, "adk_buddy")
        self.assertEqual(package_for_public_id("expertbuddy-adk").runtime, "adk")
        self.assertEqual(package_for_public_id("expertbuddy-deepagents").runtime, "langgraph")
        self.assertIsNone(package_for_public_id("missing"))
        self.assertIsNone(package_for_public_id("deepagents"))
        self.assertIsNone(package_for_public_id("adk"))

    def test_both_agents_use_the_same_knowledge_skill(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "agents"
        adk = (root / "adk_buddy" / "skills" / "knowledge-qa" / "SKILL.md").read_text(encoding="utf-8")
        deepagents = (root / "expert_buddy" / "skills" / "knowledge-qa" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(adk, deepagents)
        self.assertEqual(package_for_public_id("expertbuddy-adk").enabled_tools, package_for_public_id("expertbuddy-deepagents").enabled_tools)
        self.assertEqual(package_for_public_id("expertbuddy-adk").enabled_tools, (
            "knowledge_discover",
            "knowledge_search",
            "list_doc_segments",
            "list_video_segments",
        ))


if __name__ == "__main__":
    unittest.main()
