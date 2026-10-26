"""Resolve chat model from a viknow_ops catalog."""

from __future__ import annotations

import os
import unittest

from viknow3.ops.config.model_resolve import (
    ChatModelRow,
    ChatProvider,
    PlatformCatalog,
    resolve_chat,
    resolve_openai_chat,
)


def _catalog() -> PlatformCatalog:
    return PlatformCatalog(
        providers={
            "private": ChatProvider(
                base_url="https://example/v1",
                api_key="secret",
                model_name="vllm/Qwen/Qwen3.6-35B-A3B-FP8",
            ),
            "siliconflow": ChatProvider(
                base_url="https://api.example/v1",
                api_key="sf-secret",
                model_name=None,
            ),
        },
        models=(
            ChatModelRow(
                id="private:qwen",
                provider_id="private",
                name="vllm/Qwen/Qwen3.6-35B-A3B-FP8",
                label="内网 Qwen",
            ),
            ChatModelRow(
                id="siliconflow:Qwen/Qwen3.6-35B-A3B",
                provider_id="siliconflow",
                name="Qwen/Qwen3.6-35B-A3B",
                label="Qwen3.6",
            ),
        ),
        default_model_id="private:qwen",
        agent_model_id="private:qwen",
    )


class ModelResolveTests(unittest.TestCase):
    def test_server_default_uses_agent_model_row(self) -> None:
        resolved = resolve_chat("server-default", _catalog(), temperature=0.7)
        self.assertEqual(resolved.model, "vllm/Qwen/Qwen3.6-35B-A3B-FP8")
        self.assertEqual(resolved.base_url, "https://example/v1")
        self.assertEqual(resolved.api_key, "secret")
        self.assertAlmostEqual(resolved.temperature, 0.7)

    def test_selector_matches_model_id(self) -> None:
        resolved = resolve_chat("siliconflow:Qwen/Qwen3.6-35B-A3B", _catalog(), temperature=0)
        self.assertEqual(resolved.model, "Qwen/Qwen3.6-35B-A3B")
        self.assertEqual(resolved.base_url, "https://api.example/v1")
        self.assertEqual(resolved.api_key, "sf-secret")

    def test_openai_chat_publishes_ops_endpoint(self) -> None:
        os.environ.pop("VIKNOW_PRIVATE_API_KEY", None)
        resolved = resolve_openai_chat("server-default", catalog=_catalog())
        self.assertEqual(resolved.api_key, "secret")
        self.assertEqual(os.environ["VIKNOW_PRIVATE_BASE_URL"], "https://example/v1")
        self.assertEqual(os.environ["VIKNOW_PRIVATE_API_KEY"], "secret")
        self.assertEqual(os.environ["VIKNOW_PRIVATE_MODEL"], "vllm/Qwen/Qwen3.6-35B-A3B-FP8")


if __name__ == "__main__":
    unittest.main()
