"""Discover agent profiles.

Profiles live in ``src/viknow3/agents/profiles``. Skills live once in ``src/viknow3/agents/skills``.
Turn loops live in ``src/viknow3/agents/runtimes``. ``runtime: adk`` selects the ADK loop.
Any other value, including the default ``langgraph``, selects DeepAgents.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


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
        return (self.profile_id,)


def agents_root() -> Path:
    return Path(__file__).resolve().parent


def skills_root() -> Path:
    return agents_root() / "skills"


@lru_cache(maxsize=1)
def discover_agents() -> tuple[AgentPackage, ...]:
    found: list[AgentPackage] = []
    skills = skills_root()
    for path in sorted((agents_root() / "profiles").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not data.get("id"):
            continue
        tools = tuple(str(item) for item in data.get("enabled_tools") or ())
        found.append(
            AgentPackage(
                directory=path.stem,
                profile_path=path,
                skills_dir=skills,
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
