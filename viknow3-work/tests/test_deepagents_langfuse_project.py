"""DeepAgents Langfuse tracing uses its own project credentials."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

from viknow3.agents.runtimes.deepagents.agent_adapters.langfuse import (
    GenerationRecorder,
    build_langfuse_callbacks,
    deepagents_langfuse_credentials,
)
from viknow3.agents.runtimes.deepagents.core.config import LangfuseTracingConfig


class DeepAgentsLangfuseProjectTests(unittest.TestCase):
    def test_legacy_langfuse_env_does_not_select_a_project(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIKNOW_LANGFUSE_ENABLED": "true",
                "VIKNOW_LANGFUSE_HOST": "http://langfuse.example",
                "VIKNOW_LANGFUSE_PUBLIC_KEY": "pk-other",
                "VIKNOW_LANGFUSE_SECRET_KEY": "sk-other",
                "LANGFUSE_PUBLIC_KEY": "pk-hyperrag",
            },
            clear=False,
        ):
            os.environ.pop("VIKNOW_DEEPAGENTS_LANGFUSE_HOST", None)
            os.environ.pop("VIKNOW_DEEPAGENTS_LANGFUSE_PUBLIC_KEY", None)
            os.environ.pop("VIKNOW_DEEPAGENTS_LANGFUSE_SECRET_KEY", None)
            self.assertIsNone(deepagents_langfuse_credentials())
            self.assertEqual(build_langfuse_callbacks(LangfuseTracingConfig(enabled=True)), [])
            self.assertEqual(os.environ["LANGFUSE_PUBLIC_KEY"], "pk-hyperrag")

    def test_dedicated_credentials_build_an_isolated_client(self) -> None:
        created: dict[str, str] = {}
        handlers: list[str] = []

        class FakeLangfuse:
            def __init__(self, **kwargs: str) -> None:
                created.update(kwargs)

        class FakeHandler:
            def __init__(self, *, public_key: str) -> None:
                handlers.append(public_key)

        langfuse_mod = types.ModuleType("langfuse")
        langfuse_mod.Langfuse = FakeLangfuse
        langchain_mod = types.ModuleType("langfuse.langchain")
        langchain_mod.CallbackHandler = FakeHandler
        with patch.dict(
            os.environ,
            {
                "VIKNOW_DEEPAGENTS_LANGFUSE_HOST": "http://127.0.0.1:3000",
                "VIKNOW_DEEPAGENTS_LANGFUSE_PUBLIC_KEY": "pk-lf-deepagents",
                "VIKNOW_DEEPAGENTS_LANGFUSE_SECRET_KEY": "sk-lf-deepagents",
                "LANGFUSE_PUBLIC_KEY": "pk-hyperrag",
            },
            clear=False,
        ), patch.dict(sys.modules, {"langfuse": langfuse_mod, "langfuse.langchain": langchain_mod}):
            callbacks = build_langfuse_callbacks(LangfuseTracingConfig())
            self.assertEqual(handlers, ["pk-lf-deepagents"])
            self.assertEqual(created["public_key"], "pk-lf-deepagents")
            self.assertEqual(created["secret_key"], "sk-lf-deepagents")
            self.assertEqual(created["host"], "http://127.0.0.1:3000")
            self.assertEqual(created["environment"], "viknow3")
            self.assertIs(callbacks[0].__class__, FakeHandler)
            self.assertEqual(os.environ["LANGFUSE_PUBLIC_KEY"], "pk-hyperrag")

    def test_stock_handler_without_record_generation_is_quiet(self) -> None:
        recorder = GenerationRecorder("run-1", handler=object())
        recorder.record(name="inspect", model="qwen", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        self.assertEqual(recorder.breakdown()["totals"]["total_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
