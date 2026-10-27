"""Agent packages share one runtime and stay addressable by public id."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
