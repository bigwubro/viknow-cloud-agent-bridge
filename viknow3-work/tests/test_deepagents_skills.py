"""Deepagents skills staging and session workspace backend."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIKNOW_USE_SCRIPT_MODEL", "1")

from deepagents.backends.composite import CompositeBackend
from deepagents.middleware.skills import SkillsMiddleware, _list_skills
from langgraph.checkpoint.memory import MemorySaver

from viknow3.application.agent.qa_box.backends import build_filesystem_workspace_backend
from viknow3.application.agent.qa_box.builder import build_graph
from viknow3.application.agent.qa_box.profile import load_viknow_v2_profile
from viknow3.application.agent.qa_box.workspace_config import session_workspace_dir
from viknow3.application.agent.qa_box.bubblewrap_workspace import prepare_host_staging
from viknow3.application.agent.qa_box.workspace_staging import workspace_skills_source


class DeepagentsSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["VIKNOW_AGENT_WORKSPACE_ROOT"] = self._tmp.name
        self._profile = load_viknow_v2_profile()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_staged_skills_on_disk(self) -> None:
        prepare_host_staging("disk-session", self._profile.enabled_skills)
        ws = session_workspace_dir("disk-session")
        for name in self._profile.enabled_skills:
            skill_md = ws / ".skills" / name / "SKILL.md"
            self.assertTrue(skill_md.is_file(), msg=str(skill_md))

    def test_skills_middleware_lists_profile_skills(self) -> None:
        prepare_host_staging("skills-test-session", self._profile.enabled_skills)
        backend = build_filesystem_workspace_backend("skills-test-session")
        source = workspace_skills_source("filesystem")
        skills = _list_skills(backend, source)
        names = {item["name"] for item in skills}
        self.assertEqual(names, set(self._profile.enabled_skills))

    def test_workspace_paths_not_repo_root(self) -> None:
        prepare_host_staging("route-test-session", self._profile.enabled_skills)
        backend = build_filesystem_workspace_backend("route-test-session")
        self.assertIsInstance(backend, CompositeBackend)
        ws_dir = session_workspace_dir("route-test-session")
        (ws_dir / "outputs" / "marker.txt").write_text("ok", encoding="utf-8")
        read = backend.read("/workspace/outputs/marker.txt")
        self.assertNotIn("Error", read[:20] if isinstance(read, str) else str(read)[:20])
        skill_read = backend.read("/workspace/.skills/knowledge-qa-v2/SKILL.md")
        self.assertNotIn("Error", str(skill_read)[:30])

    def test_built_graph_uses_workspace_skill_source(self) -> None:
        prepare_host_staging("graph-skills-session", self._profile.enabled_skills)
        graph = build_graph(None, MemorySaver(), "graph-skills-session")
        node_names = [name for name in graph.nodes if "SkillsMiddleware" in name]
        self.assertTrue(node_names)
        backend = build_filesystem_workspace_backend("graph-skills-session")
        source = workspace_skills_source("filesystem")
        middleware = SkillsMiddleware(backend=backend, sources=[source])
        skills = _list_skills(middleware._backend, source)
        names = {item["name"] for item in skills}
        self.assertEqual(names, set(self._profile.enabled_skills))


if __name__ == "__main__":
    unittest.main()
