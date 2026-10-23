"""library_id resolution aligned with viknow2 tool handlers."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from viknow3.application.agent.knowledge_assistant.library_scope import resolve_library_id
from viknow3.application.agent.knowledge_assistant.tools import knowledge_search
from viknow3.application.agent.qa_expert.grant import bind_grant, reset_grant


class _Grant:
    def __init__(self, libraries: list[str]) -> None:
        self._libraries = libraries

    def libraries(self) -> list[str]:
        return self._libraries


class LibraryScopeTests(unittest.TestCase):
    def test_resolve_prefers_explicit_id(self) -> None:
        token = bind_grant(_Grant(["lib-a"]))
        try:
            self.assertEqual(resolve_library_id("lib-b"), "lib-b")
        finally:
            reset_grant(token)

    def test_resolve_single_grant_without_arg(self) -> None:
        token = bind_grant(_Grant(["video123"]))
        try:
            self.assertEqual(resolve_library_id(""), "video123")
        finally:
            reset_grant(token)

    def test_knowledge_search_omits_library_id_on_single_grant(self) -> None:
        token = bind_grant(_Grant(["lib-a"]))
        try:
            with patch("viknow3.application.hyperrag.client.HYPERAG.search") as search:
                search.return_value = {"library_id": "lib-a", "query": "q", "evidence": [], "source": "hyperrag"}
                payload = json.loads(knowledge_search(query="q"))
            self.assertNotIn("error", payload)
            self.assertEqual(search.call_args.args[0], "lib-a")
        finally:
            reset_grant(token)


if __name__ == "__main__":
    unittest.main()
