"""Agent packages share one runtime and stay addressable by public id."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from agents.layout import discover_agents, package_for_public_id


class AgentLayoutTests(unittest.TestCase):
    def test_public_ids(self) -> None:
        ids = [public for package in discover_agents() for public in package.public_ids()]
        self.assertEqual(
            ids,
            [
                "adk",
                "mmlongbench-doc-eval",
                "mmlongbench-doc-extract",
                "evidence-oracle",
                "deepagents",
                "ops",
                "viknow-v2-advanced-video",
            ],
        )
        self.assertEqual(package_for_public_id("deepagents").directory, "expert_buddy")
        self.assertEqual(package_for_public_id("adk").directory, "adk_buddy")
        self.assertEqual(package_for_public_id("adk").runtime, "adk")
        self.assertEqual(package_for_public_id("deepagents").runtime, "langgraph")
        self.assertIsNone(package_for_public_id("missing"))

    def test_evidence_oracle_uses_a_profile_flag(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agents"
            / "evidence_oracle"
            / "profile"
            / "evidence-oracle.yaml"
        )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertTrue(data["oracle_collection"])
        self.assertTrue(data["multi_agent_enabled"])
        sidecar = path.parents[1] / "subagents" / "evidence-oracle.yaml"
        subagents = yaml.safe_load(sidecar.read_text(encoding="utf-8"))["subagents"]
        self.assertEqual([item["name"] for item in subagents], ["evidence-scout", "evidence-inspector"])


if __name__ == "__main__":
    unittest.main()
