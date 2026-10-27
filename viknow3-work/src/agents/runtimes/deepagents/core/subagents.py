"""Load and validate DeepAgents specialist sidecar configs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.runtimes.deepagents.core.skills import SkillSpec, resolve_enabled_skills

KNOWLEDGE_QA_SKILL = "knowledge-qa-v2"
ORCHESTRATOR_SKILL = "knowledge-qa-orchestrate"
ORACLE_COLLECTION_SKILL = "evidence-oracle-collection"

_PROFILE_ORCHESTRATOR_SKILLS = {
    KNOWLEDGE_QA_SKILL: ORCHESTRATOR_SKILL,
    ORACLE_COLLECTION_SKILL: ORACLE_COLLECTION_SKILL,
}


class SubAgentSpec(BaseModel):
    """Declarative specialist used when multi-agent delegation is enabled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    enabled_tools: tuple[str, ...] = ()
    enabled_skills: tuple[str, ...] = ()


def subagents_dir_for_profiles(profile_dir: Path) -> Path:
    """Return the sidecar directory that sits next to ``profiles/``."""

    return profile_dir.parent / "subagents"


def load_profile_subagents(profile_id: str, profile_dir: Path) -> tuple[SubAgentSpec, ...]:
    """Load specialist specs for one session profile. Missing files are empty."""

    path = subagents_dir_for_profiles(profile_dir) / f"{profile_id}.yaml"
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"subagent sidecar must contain a mapping: {path}")
    items = raw.get("subagents", [])
    if items is None:
        return ()
    if not isinstance(items, list):
        raise ValueError(f"subagents must be a list: {path}")
    specs = tuple(SubAgentSpec.model_validate(item) for item in items)
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate subagent name(s) in {path}: {', '.join(duplicates)}")
    return specs


def validate_profile_subagents(
    specs: Sequence[SubAgentSpec],
    *,
    profile_id: str,
    supported_tools: set[str],
    skill_dirs: Sequence[Path],
    profile_skills: Sequence[str] = (),
) -> None:
    """Validate specialist tools and skill dependencies for one profile."""

    for spec in specs:
        unknown = sorted(set(spec.enabled_tools) - supported_tools)
        if unknown:
            raise RuntimeError(
                f"profile {profile_id} subagent {spec.name} has unsupported tools: "
                f"{', '.join(unknown)}"
            )
        try:
            resolve_enabled_skills(
                skill_dirs,
                spec.enabled_skills,
                enabled_tools=spec.enabled_tools,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"profile {profile_id} subagent {spec.name}: {exc}") from exc
    if specs:
        orchestrator = main_agent_skill_names(profile_skills, specs)[0]
        try:
            resolve_enabled_skills(skill_dirs, (orchestrator,))
        except RuntimeError as exc:
            raise RuntimeError(f"profile {profile_id}: {exc}") from exc


def specialist_tool_names(specs: Sequence[SubAgentSpec]) -> frozenset[str]:
    """Return tool ids reserved for specialists."""

    return frozenset(tool_id for spec in specs for tool_id in spec.enabled_tools)


def exclusive_specialist_tool_owners(specs: Sequence[SubAgentSpec]) -> dict[str, str]:
    """Map a tool id to the only specialist that owns it.

    Shared tools are omitted so the caller falls back to event metadata.
    """

    owners: dict[str, set[str]] = {}
    for spec in specs:
        for tool_id in spec.enabled_tools:
            owners.setdefault(tool_id, set()).add(spec.name)
    return {tool_id: next(iter(names)) for tool_id, names in owners.items() if len(names) == 1}


def specialist_skill_names(specs: Sequence[SubAgentSpec]) -> frozenset[str]:
    """Return skill names reserved for specialists."""

    return frozenset(skill_name for spec in specs for skill_name in spec.enabled_skills)


def main_agent_skill_names(
    profile_skill_names: Sequence[str],
    specs: Sequence[SubAgentSpec],
) -> tuple[str, ...]:
    """Select the profile's main orchestration skill when specialists are active."""

    selected = [
        _PROFILE_ORCHESTRATOR_SKILLS[skill_name]
        for skill_name in profile_skill_names
        if skill_name in _PROFILE_ORCHESTRATOR_SKILLS
    ]
    if len(selected) > 1:
        raise RuntimeError("a multi-agent profile may declare only one orchestration skill")
    orchestrator_skill = selected[0] if selected else ORCHESTRATOR_SKILL

    reserved = specialist_skill_names(specs) | set(_PROFILE_ORCHESTRATOR_SKILLS)
    kept = tuple(name for name in profile_skill_names if name not in reserved)
    return (orchestrator_skill, *kept)


def filter_main_agent_tools(tools: Sequence[Any], specs: Sequence[SubAgentSpec]) -> list[Any]:
    """Drop tools that specialists own so the main agent must delegate."""

    reserved = specialist_tool_names(specs)
    if not reserved:
        return list(tools)
    return [tool for tool in tools if str(getattr(tool, "name", "") or "") not in reserved]


def resolve_orchestrated_main_skills(
    skill_dirs: Sequence[Path],
    profile_skill_names: Sequence[str],
    specs: Sequence[SubAgentSpec],
    *,
    enabled_tools: Sequence[str] = (),
) -> list[SkillSpec]:
    """Load main-agent skills for an orchestrated multi-agent run."""

    return resolve_enabled_skills(
        skill_dirs,
        main_agent_skill_names(profile_skill_names, specs),
        enabled_tools=enabled_tools,
    )
