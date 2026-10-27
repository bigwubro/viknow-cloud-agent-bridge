"""Config tables against the dedicated ops Postgres test database."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import psycopg

_env = Path.home() / ".viknow3" / "ops.env"
if _env.is_file():
    _vals = {}
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            _vals[_key] = _value
    if _vals.get("VIKNOW_OPS_TEST_DSN"):
        os.environ["VIKNOW_OPS_DSN"] = _vals["VIKNOW_OPS_TEST_DSN"]
    if _vals.get("VIKNOW_OPS_SECRET"):
        os.environ["VIKNOW_OPS_SECRET"] = _vals["VIKNOW_OPS_SECRET"]

from viknow3.application.agentcore.registry.catalog import Registry
from viknow3.ops.config.store import ConfigError, reset_for_tests, set_value, snapshot


class OpsConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_for_tests()

    def test_secret_is_stored_encrypted_and_not_returned(self) -> None:
        updated = set_value("model_provider", "api_key", "plain-key", "private", "admin")
        self.assertEqual(updated["api_key"], {"set": True})
        with psycopg.connect(os.environ["VIKNOW_OPS_DSN"]) as conn:
            conn.execute("SET search_path TO ops, public")
            stored = conn.execute("SELECT api_key FROM model_provider WHERE id = 'private'").fetchone()[0]
        self.assertNotIn("plain-key", stored)
        from viknow3.ops.config.store import changes, revert

        row = changes()[0]
        self.assertTrue(row["secret"])
        self.assertIsNone(row["previous_value"])
        with self.assertRaises(ConfigError) as caught:
            revert(row["id"], "admin")
        self.assertEqual(caught.exception.status, 400)

    def test_agent_primary_model_is_visible_to_qa_profiles(self) -> None:
        set_value("agent_model", "model_id", "siliconflow:Qwen/Qwen3.6-35B-A3B", "expertbuddy-deepagents", "admin")
        profile = Registry().profile("expertbuddy-deepagents")
        assert profile is not None
        self.assertEqual(profile.public()["default_model"], "siliconflow:Qwen/Qwen3.6-35B-A3B")
        self.assertTrue(Registry().known_model("private:qwen"))
        self.assertFalse(Registry().known_model("missing"))

    def test_rejected_choice_does_not_write(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            set_value("retrieval_setting", "mode", "keyword", None, "admin")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(snapshot()["retrieval_setting"]["mode"], "hybrid")
