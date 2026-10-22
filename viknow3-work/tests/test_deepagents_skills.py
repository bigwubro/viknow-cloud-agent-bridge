"""Deepagents skills loading and session workspace backend."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware, _list_skills
from langgraph.checkpoint.memory import MemorySaver

from viknow3.application.agent.knowledge_assistant.backends import build_deepagents_backend
from viknow3.application.agent.knowledge_assistant.builder import build_graph
from viknow3.application.agent.knowledge_assistant.workspace_config import session_workspace_dir


class DeepagentsSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_skills_middleware_lists_profile_skills(self) -> None:
        backend = build_deepagents_backend("skills-test-session")
        skills = _list_skills(backend, "/")
        names = {item["name"] for item in skills}
        self.assertIn("knowledge-qa-v2", names)
        self.assertIn("visual-ground", names)

    def test_composite_routes_workspace_not_repo_root(self) -> None:
        backend = build_deepagents_backend("route-test-session")
        self.assertIsInstance(backend, CompositeBackend)
        ws_dir = session_workspace_dir("route-test-session")
        (ws_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (ws_dir / "outputs" / "marker.txt").write_text("ok", encoding="utf-8")
        read = backend.read("/workspace/outputs/marker.txt")
        self.assertNotIn("Error", read[:20] if isinstance(read, str) else str(read)[:20])
        globbed = backend.glob("**/*")
        if hasattr(globbed, "matches"):
            paths = [item["path"] for item in globbed.matches]
        elif hasattr(globbed, "entries"):
            paths = [item["path"] for item in globbed.entries]
        else:
            paths = [item["path"] for item in globbed]
        repo_markers = [p for p in paths if "pyproject.toml" in p or "viknow3/application" in p]
        self.assertEqual(repo_markers, [])

    def test_built_graph_uses_root_skill_source(self) -> None:
        graph = build_graph(None, MemorySaver(), "graph-skills-session")
        node_names = [name for name in graph.nodes if "SkillsMiddleware" in name]
        self.assertTrue(node_names)
        backend = build_deepagents_backend("graph-skills-session")
        middleware = SkillsMiddleware(backend=backend, sources=["/"])
        skills = _list_skills(middleware._backend, "/")
        names = {item["name"] for item in skills}
        self.assertEqual(names, {"knowledge-qa-v2", "visual-ground"})

    def test_default_backend_is_skills_tree_only(self) -> None:
        backend = build_deepagents_backend("skills-only-session")
        assert isinstance(backend, CompositeBackend)
        default = backend.default
        assert isinstance(default, FilesystemBackend)
        ls = default.ls("/")
        entries = ls.entries if hasattr(ls, "entries") else ls
        dir_names = {Path(item["path"]).name for item in entries if item.get("is_dir")}
        self.assertIn("knowledge-qa-v2", dir_names)
        self.assertNotIn("src", dir_names)


if __name__ == "__main__":
    unittest.main()
