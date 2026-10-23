"""Resolve chat model from app.yaml + env (5175 private Qwen path)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from viknow3.ops.config.model_resolve import resolve_openai_chat


class ModelResolveTests(unittest.TestCase):
    def test_server_default_uses_private_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "app.yaml"
            cfg.write_text(
                """
share:
  model_providers:
    private:
      base_url_env: VIKNOW_PRIVATE_BASE_URL
      api_key_env: VIKNOW_PRIVATE_API_KEY
  agent:
    model_ref: private:${VIKNOW_PRIVATE_MODEL}
agent:
  model:
    temperature: 0.7
""".strip(),
                encoding="utf-8",
            )
            os.environ["VIKNOW_APP_CONFIG"] = str(cfg)
            os.environ["VIKNOW_PRIVATE_BASE_URL"] = "https://example/v1"
            os.environ["VIKNOW_PRIVATE_API_KEY"] = "secret"
            os.environ["VIKNOW_PRIVATE_MODEL"] = "vllm/Qwen/Qwen3.6-35B-A3B-FP8"
            resolved = resolve_openai_chat("server-default")
            self.assertEqual(resolved.model, "vllm/Qwen/Qwen3.6-35B-A3B-FP8")
            self.assertEqual(resolved.base_url, "https://example/v1")
            self.assertEqual(resolved.api_key, "secret")
            self.assertAlmostEqual(resolved.temperature, 0.7)


if __name__ == "__main__":
    unittest.main()
