"""Agent packages share one runtime and stay addressable by public id."""

from __future__ import annotations

import unittest
from pathlib import Path

from viknow3.agents.layout import discover_agents, package_for_public_id, skills_root


def root_skills() -> Path:
    return skills_root()


class AgentLayoutTests(unittest.TestCase):
    def test_public_ids(self) -> None:
        ids = [public for package in discover_agents() for public in package.public_ids()]
        self.assertEqual(
            ids,
            [
                "expertbuddy-deepagents",
                "expertbuddy-adk",
            ],
        )
        deepagents = package_for_public_id("expertbuddy-deepagents")
        adk = package_for_public_id("expertbuddy-adk")
        self.assertEqual(deepagents.directory, "expert_buddy")
        self.assertEqual(adk.directory, "expert_buddy_adk")
        self.assertEqual(deepagents.skills_dir, adk.skills_dir)
        self.assertEqual(deepagents.skills_dir, root_skills())
        self.assertEqual(package_for_public_id("expertbuddy-adk").runtime, "adk")
        self.assertEqual(package_for_public_id("expertbuddy-deepagents").runtime, "langgraph")
        self.assertIsNone(package_for_public_id("missing"))
        self.assertIsNone(package_for_public_id("deepagents"))
        self.assertIsNone(package_for_public_id("adk"))

    def test_both_agents_use_the_same_knowledge_skill(self) -> None:
        skills = root_skills()
        for name in ("knowledge-qa-v2", "visual-ground"):
            self.assertTrue((skills / name / "SKILL.md").is_file())
        skill = (skills / "knowledge-qa-v2" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("0.31.0-final-check-tool", skill)
        self.assertIn("final_check()", skill)
        tools = (
            "knowledge_discover",
            "uploaded_image_search",
            "answer_plan",
            "knowledge_search",
            "video_inspect",
            "visual_ground",
            "document_inspect",
            "list_video_segments",
            "list_doc_segments",
        )
        self.assertEqual(package_for_public_id("expertbuddy-adk").enabled_tools, package_for_public_id("expertbuddy-deepagents").enabled_tools)
        self.assertEqual(package_for_public_id("expertbuddy-adk").enabled_tools, tools)


if __name__ == "__main__":
    unittest.main()
