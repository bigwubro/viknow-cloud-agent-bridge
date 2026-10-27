"""Agent runtime config is assembled from ops catalog plus runtime.yaml."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from agents.runtimes.deepagents.runtime_config import (
    RUNTIME_CONFIG_PATH,
    load_model_defaults,
    load_runtime_document,
)
from viknow3.ops.config.model_resolve import ChatModelRow, ChatProvider, PlatformCatalog


def _host_config():
    """Load the assembler without the shell package, which opens Postgres on import."""
    try:
        import scheduler_core  # noqa: F401
    except ImportError:
        return None
    path = (
        Path(__file__).resolve().parents[1]
        / "src/viknow3/application/agent/deepagents_adapter/vendor/host_config.py"
    )
    spec = importlib.util.spec_from_file_location("agent_host_config_under_test", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog() -> PlatformCatalog:
    return PlatformCatalog(
        providers={
            "private": ChatProvider(
                base_url="https://example/v1",
                api_key="secret",
                model_name="vllm/Qwen/Qwen3.6-35B-A3B-FP8",
            )
        },
        models=(
            ChatModelRow(
                id="private:qwen",
                provider_id="private",
                name="vllm/Qwen/Qwen3.6-35B-A3B-FP8",
                label="内网 Qwen",
            ),
        ),
        default_model_id="private:qwen",
        agent_model_id="private:qwen",
    )


class AgentHostConfigTests(unittest.TestCase):
    def test_reset_checkpoint_tables_refuses_a_non_test_database(self) -> None:
        from agents.runtimes.deepagents.agent_adapters.checkpointer import reset_checkpoint_tables

        with self.assertRaises(RuntimeError):
            reset_checkpoint_tables("postgresql://example/agent")
    def test_runtime_yaml_holds_shared_loop_settings(self) -> None:
        raw = load_runtime_document()
        defaults = load_model_defaults()
        self.assertEqual(raw["recursion_limit"], 999)
        self.assertEqual(raw["workspace"]["backend"], "bubblewrap")
        self.assertTrue(raw["skill_reminder"]["enabled"])
        self.assertAlmostEqual(defaults.temperature, 0.7)
        text = RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("base_url", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("model_providers", text)

    def test_app_config_uses_ops_model_and_runtime_yaml(self) -> None:
        host = _host_config()
        if host is None:
            self.skipTest("scheduler_core is installed with the agent runtime")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = host.build_agent_app_config(
                skills_dir=root / "skills",
                profile_dir=root / "profile",
                agent_ids=("deepagents", "expert_buddy"),
                files_root=root / "files",
                catalog=_catalog(),
                static_dir=root / "ui",
            )
        self.assertEqual(config.agent.model.ref, "private:vllm/Qwen/Qwen3.6-35B-A3B-FP8")
        self.assertEqual(config.agent.deepagents.recursion_limit, 999)
        self.assertTrue(config.agent.deepagents.checkpointer.enabled)
        self.assertEqual(config.agent.deepagents.checkpointer.backend.value, "postgres")
        self.assertTrue(config.agent.deepagents.checkpointer.postgres_dsn)
        self.assertFalse(config.share.scheduler.unified.enabled)
        self.assertEqual(config.share.model_providers["private"].base_url_env, "VIKNOW_PRIVATE_BASE_URL")
        self.assertEqual(config.share.model_providers["private"].api_key_env, "VIKNOW_PRIVATE_API_KEY")
        self.assertEqual(config.config_path, RUNTIME_CONFIG_PATH)
        self.assertEqual(config.agent.profile_dir, root / "profile")
        tools = host.review_tool_configs(config)
        assert tools.multimodal is not None
        self.assertEqual(tools.multimodal.review_model.require_model(), "vllm/Qwen/Qwen3.6-35B-A3B-FP8")
        self.assertEqual(tools.multimodal.review_model.resolve_base_url(), "https://example/v1")


if __name__ == "__main__":
    unittest.main()