"""Harness tool gate rules (viknow2 subset)."""

from __future__ import annotations

import unittest

from viknow3.application.agentcore.harness.facts import HarnessRunFacts
from viknow3.application.agentcore.harness.tool_gate import evaluate_tool_gate


class HarnessToolGateTests(unittest.TestCase):
    def test_blocks_workflow_before_skill_loaded(self) -> None:
        facts = HarnessRunFacts(library_ids=["lib-a"])
        decision = evaluate_tool_gate("knowledge_discover", {}, facts, primary_skill="knowledge-qa-v2")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule, "required_skill_not_loaded")

    def test_blocks_workspace_guess_when_library_granted(self) -> None:
        facts = HarnessRunFacts(library_ids=["lib-a"], skill_loaded=True)
        decision = evaluate_tool_gate("ls", {"path": "/workspace"}, facts, primary_skill="knowledge-qa-v2")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule, "kb_first_no_workspace_guess")

    def test_allows_read_skill_path(self) -> None:
        facts = HarnessRunFacts(library_ids=["lib-a"])
        decision = evaluate_tool_gate(
            "read_file",
            {"path": "/workspace/.skills/knowledge-qa-v2/SKILL.md"},
            facts,
            primary_skill="knowledge-qa-v2",
        )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
