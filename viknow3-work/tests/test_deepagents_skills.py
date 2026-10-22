"""Deepagents skills staging and session workspace backend (viknow2 parity)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from deepagents.backends.composite import CompositeBackend
from deepagents.middleware.skills import SkillsMiddleware, _list_skills
from langgraph.checkpoint.memory import MemorySaver

from viknow3.application.agent.knowledge_assistant.backends import build_deepagents_backend, workspace_skills_source
from viknow3.application.agent.knowledge_assistant.builder import build_graph
from viknow3.application.agent.knowledge_assistant.profile import load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.session_workspace import stage_runtime_skills
from viknow3.application.agent.knowledge_assistant.workspace_config import session_workspace_dir


class DeepagentsSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_staged_skills_on_disk(self) -> None:
        stage_runtime_skills("disk-session", self._profile.enabled_skills)
        ws = session_workspace_dir("disk-session")
        for name in self._profile.enabled_skills:
            skill_md = ws / ".skills" / name / "SKILL.md"
            self.assertTrue(skill_md.is_file(), msg=str(skill_md))

    def test_skills_middleware_lists_profile_skills(self) -> None:
        backend = build_deepagents_backend("skills-test-session", self._profile.enabled_skills)
        skills = _list_skills(backend, workspace_skills_source())
        names = {item["name"] for item in skills}
        self.assertEqual(names, set(self._profile.enabled_skills))

    def test_workspace_paths_not_repo_root(self) -> None:
        backend = build_deepagents_backend("route-test-session", self._profile.enabled_skills)
        self.assertIsInstance(backend, CompositeBackend)
        ws_dir = session_workspace_dir("route-test-session")
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
        skill_paths = [p for p in paths if ".skills" in p and "SKILL.md" in p]
        self.assertTrue(skill_paths)

    def test_built_graph_uses_workspace_skill_source(self) -> None:
        graph = build_graph(None, MemorySaver(), "graph-skills-session")
        node_names = [name for name in graph.nodes if "SkillsMiddleware" in name]
        self.assertTrue(node_names)
        backend = build_deepagents_backend("graph-skills-session", self._profile.enabled_skills)
        middleware = SkillsMiddleware(backend=backend, sources=[workspace_skills_source()])
        skills = _list_skills(middleware._backend, workspace_skills_source())
        names = {item["name"] for item in skills}
        self.assertEqual(names, set(self._profile.enabled_skills))


if __name__ == "__main__":
    unittest.main()
