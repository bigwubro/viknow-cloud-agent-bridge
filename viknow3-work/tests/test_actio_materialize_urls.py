from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from viknow3.ops.config.runtime_env import load_agent_runtime_env

from viknow3.application.hyperrag.actio_materialize import (
    _materialize_embedding_base_url,
    _materialize_private_llm_base_url,
)


class ActioMaterializeUrlTest(unittest.TestCase):
    def test_private_prefers_deploy_env_over_localhost_ops(self) -> None:
        with patch.dict(
            os.environ,
            {"VIKNOW_PRIVATE_BASE_URL": "https://ai.qingxiang.tech:8443/v1"},
            clear=False,
        ):
            base = _materialize_private_llm_base_url(
                "http://127.0.0.1:8443/v1",
                "https://ai.qingxiang.tech:8443/v1",
            )
        self.assertEqual(base, "https://ai.qingxiang.tech:8443/v1")

    def test_private_upgrades_http_8443_without_env(self) -> None:
        with patch.dict(os.environ, {"VIKNOW_PRIVATE_BASE_URL": ""}, clear=False):
            base = _materialize_private_llm_base_url(
                "http://ai.qingxiang.tech:8443/v1",
                "",
            )
        self.assertEqual(base, "https://ai.qingxiang.tech:8443/v1")

    def test_embedding_uses_embedding_env_not_chat(self) -> None:
        with patch.dict(
            os.environ,
            {"VIKNOW_EMBEDDING_BASE_URL": "http://ai.qingxiang.tech:8601/v1"},
            clear=False,
        ):
            base = _materialize_embedding_base_url("http://127.0.0.1:8601/v1")
        self.assertEqual(base, "http://ai.qingxiang.tech:8601/v1")


    def test_runtime_env_upgrades_private_8443(self) -> None:
        from viknow3.ops.config.runtime_env import _normalize_private_base_url_for_host

        with patch.dict(
            os.environ,
            {"VIKNOW_PRIVATE_BASE_URL": "http://127.0.0.1:8443/v1"},
            clear=False,
        ):
            _normalize_private_base_url_for_host()
            self.assertEqual(
                os.environ.get("VIKNOW_PRIVATE_BASE_URL"),
                "https://127.0.0.1:8443/v1",
            )


if __name__ == "__main__":
    unittest.main()
