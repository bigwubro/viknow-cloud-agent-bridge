"""Ops → HyperRAG materialization invariants."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from viknow3.service.hyperrag.ops_bootstrap import build_hyperrag_payload_from_ops


class _Bundle:
    redis_url = "redis://:secret@127.0.0.1:16379/0"
    retrieval = {}
    scheduler_unified = {"enabled": True, "max_concurrent_job_tasks": 4}
    postgres = {}
    neo4j = {}
    observe = {}
    providers = {}


class OpsBootstrapSchedulerTests(unittest.TestCase):
    @patch("viknow3.service.hyperrag.ops_bootstrap.load_runtime_bundle", return_value=_Bundle())
    def test_unified_scheduler_inherits_ops_redis_url(self, _load) -> None:
        payload = build_hyperrag_payload_from_ops()
        unified = payload["scheduler"]["unified"]
        self.assertEqual(unified["redis_url"], _Bundle.redis_url)
        self.assertEqual(payload["redis_uri"], _Bundle.redis_url)

    @patch("viknow3.service.hyperrag.ops_bootstrap.load_runtime_bundle")
    def test_embedding_model_name_is_normalized(self, load) -> None:
        bundle = _Bundle()
        bundle.providers = {"embedding": {"model_name": "Qwen3-VL-Embedding-2B", "base_url": "http://127.0.0.1:8601/v1"}}
        load.return_value = bundle
        payload = build_hyperrag_payload_from_ops()
        self.assertEqual(payload["embedding_model"], "Qwen/Qwen3-VL-Embedding-2B")

    @patch("viknow3.ops.config.model_resolve.resolve_openai_chat")
    @patch("viknow3.ops.config.store.plain_provider")
    @patch("viknow3.service.hyperrag.ops_bootstrap.load_runtime_bundle")
    def test_loopback_embedding_uses_chat_gateway_host(self, load, plain_provider, resolve_chat) -> None:
        from types import SimpleNamespace

        bundle = _Bundle()
        bundle.providers = {"embedding": {"model_name": "Qwen/Qwen3-VL-Embedding-2B", "base_url": "http://127.0.0.1:8601/v1"}}
        load.return_value = bundle
        plain_provider.return_value = bundle.providers["embedding"]
        resolve_chat.return_value = SimpleNamespace(
            base_url="https://embed-gateway.example:8443/v1",
            api_key="test-key",
        )
        payload = build_hyperrag_payload_from_ops()
        self.assertEqual(payload["embedding_base_url"], "http://embed-gateway.example:8601/v1")
        self.assertEqual(payload["embedding_api_key"], "test-key")


if __name__ == "__main__":
    unittest.main()
