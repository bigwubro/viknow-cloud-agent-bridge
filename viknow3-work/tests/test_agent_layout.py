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
        for name in ("knowledge-qa-v2", "visual-ground"):
            adk = root / "adk_buddy" / "skills" / name
            deepagents = root / "expert_buddy" / "skills" / name
            adk_files = sorted(path.relative_to(adk).as_posix() for path in adk.rglob("*") if path.is_file())
            deep_files = sorted(path.relative_to(deepagents).as_posix() for path in deepagents.rglob("*") if path.is_file())
            self.assertEqual(adk_files, deep_files)
            for relative in adk_files:
                self.assertEqual((adk / relative).read_bytes(), (deepagents / relative).read_bytes())
        skill = (root / "expert_buddy" / "skills" / "knowledge-qa-v2" / "SKILL.md").read_text(encoding="utf-8")
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
