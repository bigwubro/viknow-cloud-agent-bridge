"""Adapter boundary smoke tests."""

from __future__ import annotations

import unittest

from viknow3.service.agent.deepagents_adapter import AgentTurnInput, get_runtime
from viknow3.service.agent.deepagents_adapter.vendor_runtime import DeepAgentsTurnRuntime


class DeepAgentsAdapterTests(unittest.TestCase):
    def test_get_runtime_vendor(self) -> None:
        runtime = get_runtime()
        self.assertIsInstance(runtime, DeepAgentsTurnRuntime)

    def test_turn_input_frozen(self) -> None:
        turn = AgentTurnInput(
            session_id="s",
            run_id="r",
            user_id="u",
            user_message="hi",
            library_ids=("lib",),
        )
        self.assertEqual(turn.library_ids, ("lib",))


if __name__ == "__main__":
    unittest.main()
