"""Discover agent packages.

Each agent is a directory under ``src/agents`` with ``profile/*.yaml``.
``runtime`` is the DeepAgents loop and is not an agent. ``expert_buddy`` stays
reachable as the playground id ``deepagents``. A profile may name its own
``runtime``; the default is ``langgraph``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_SKIP = {"runtime", "__pycache__"}
_PLAYGROUND_ALIAS = {"expert_buddy": "deepagents"}


@dataclass(frozen=True)
class AgentPackage:
    directory: str
    profile_path: Path
    skills_dir: Path
    profile_id: str
    label: str
    description: str
    runtime: str
    enabled_tools: tuple[str, ...]

    def public_ids(self) -> tuple[str, ...]:
        alias = _PLAYGROUND_ALIAS.get(self.directory)
        if alias and alias != self.profile_id:
            return (alias,)
        return (self.profile_id,)


def agents_root() -> Path:
    return Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def discover_agents() -> tuple[AgentPackage, ...]:
    found: list[AgentPackage] = []
    root = agents_root()
    for child in sorted(path for path in root.iterdir() if path.is_dir()):
        if child.name in _SKIP or child.name.startswith("."):
            continue
        profile_dir = child / "profile"
        if not profile_dir.is_dir():
            continue
        for path in sorted(profile_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict) or not data.get("id"):
                continue
            tools = tuple(str(item) for item in data.get("enabled_tools") or ())
            found.append(
                AgentPackage(
                    directory=child.name,
                    profile_path=path,
                    skills_dir=child / "skills",
                    profile_id=str(data["id"]),
                    label=str(data.get("label") or data["id"]),
                    description=str(data.get("description") or ""),
                    runtime=str(data.get("runtime") or "langgraph"),
                    enabled_tools=tools,
                )
            )
    return tuple(found)


def package_for_public_id(public_id: str) -> AgentPackage | None:
    for package in discover_agents():
        if public_id in package.public_ids():
            return package
    return None
