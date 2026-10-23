"""Adapter boundary smoke tests."""

from __future__ import annotations

import unittest

from viknow3.application.agent.viknow2_adapter import (
    AgentTurnInput,
    Viknow2CompatRuntime,
    get_runtime,
)


class Viknow2AdapterTests(unittest.TestCase):
    def test_get_runtime_compat_default(self) -> None:
        runtime = get_runtime()
        self.assertIsInstance(runtime, Viknow2CompatRuntime)

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
