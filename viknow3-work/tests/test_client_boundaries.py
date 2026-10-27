"""Callers use the HyperRAG and Actio clients. They do not import those packages."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HYPER_SRC = ROOT / "external" / "HyperRAG" / "src"

_ACTIO_IMPORT = re.compile(
    r"""(?:^from\s+actio\b|^import\s+actio\b|import_module\(\s*['\"]actio|load_actio\(\s*['\"]actio)"""
)
_HYPERRAG_IMPORT = re.compile(r"""(?:^from\s+hyperrag\b|^import\s+hyperrag\b|import_module\(\s*['\"]hyperrag)""")
_VIKNOW_IMPORT = re.compile(r"""(?:^from\s+viknow3\b|^import\s+viknow3\b|import_module\(\s*['\"]viknow3)""")
_DEEP_IMPORT = re.compile(
    r"""(?:^from\s+viknow3\.agents\.runtimes\.deepagents\b|^import\s+viknow3\.agents\.runtimes\.deepagents\b|^from\s+deepagents\b|^import\s+deepagents\b)"""
)
_ADK_IMPORT = re.compile(
    r"""(?:^from\s+viknow3\.agents\.runtimes\.adk\b|^import\s+viknow3\.agents\.runtimes\.adk\b|^from\s+google\.adk\b|^import\s+google\.adk\b)"""
)
_SERVICE_IMPORT = re.compile(r"""(?:^from\s+viknow3\.service\b|^import\s+viknow3\.service\b)""")
_ACCESS_IMPORT = re.compile(r"""(?:^from\s+viknow3\.access\b|^import\s+viknow3\.access\b)""")


def _python_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def _code(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _hits(paths: list[Path], pattern: re.Pattern[str], *, allow: Path | None = None) -> list[str]:
    found: list[str] = []
    for path in paths:
        if allow is not None and path.is_relative_to(allow):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(_code(line)):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    return found


def test_actio_package_is_imported_only_by_its_client():
    roots = [ROOT / "src", HYPER_SRC]
    files = [path for root in roots for path in _python_files(root)]
    hits = _hits(files, _ACTIO_IMPORT, allow=ROOT / "src" / "viknow3" / "service" / "actio")
    assert hits == []


def test_hyperrag_package_is_imported_only_by_its_client():
    files = _python_files(ROOT / "src")
    hits = _hits(files, _HYPERRAG_IMPORT, allow=ROOT / "src" / "viknow3" / "service" / "hyperrag")
    assert hits == []


def test_agents_and_hyperrag_do_not_import_the_shell():
    agents = ROOT / "src" / "viknow3" / "agents"
    assert _hits(_python_files(agents), _SERVICE_IMPORT) == []
    access_hits = _hits(_python_files(agents), _ACCESS_IMPORT)
    assert access_hits == [
        "src/viknow3/agents/runtimes/deepagents/core/knowledge/object_key.py:35"
    ]
    assert _hits(_python_files(HYPER_SRC), _VIKNOW_IMPORT) == []


def test_adk_does_not_import_deepagents():
    hits = _hits(_python_files(ROOT / "src" / "viknow3" / "agents" / "runtimes" / "adk"), _DEEP_IMPORT)
    hits += _hits(
        _python_files(ROOT / "src" / "viknow3" / "service" / "agent" / "adk_adapter"),
        _DEEP_IMPORT,
    )
    # The ADK adapter reuses the DeepAgents tool context. The ADK runtime does not.
    assert hits == [
        "src/viknow3/service/agent/adk_adapter/inspect.py:34",
        "src/viknow3/service/agent/adk_adapter/inspect.py:102",
    ]


def test_deepagents_does_not_import_adk():
    hits = _hits(_python_files(ROOT / "src" / "viknow3" / "agents" / "runtimes" / "deepagents"), _ADK_IMPORT)
    hits += _hits(
        _python_files(ROOT / "src" / "viknow3" / "service" / "agent" / "deepagents_adapter"),
        _ADK_IMPORT,
    )
    assert hits == []


def test_unset_actio_port_does_not_invent_a_parser():
    import importlib.util

    path = HYPER_SRC / "hyperrag" / "actio_port.py"
    spec = importlib.util.spec_from_file_location("viknow_actio_port_under_test", path)
    assert spec is not None and spec.loader is not None
    port = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(port)
    port.reset()
    assert port.video_parser_class() is None
    with pytest.raises(RuntimeError, match="actio client is not installed"):
        port.normalize_global_context({})


def test_multimodal_tools_require_the_injected_actio_client():
    pytest.importorskip("scheduler_core")
    from viknow3.agents.runtimes.deepagents.core.tools.knowledge import KnowledgeToolHandlers
    from viknow3.agents.runtimes.deepagents.core.tools.multimodal import MultimodalToolHandlers

    handlers = MultimodalToolHandlers(knowledge=KnowledgeToolHandlers())
    with pytest.raises(RuntimeError, match="actio client is required"):
        handlers._require_actio()
