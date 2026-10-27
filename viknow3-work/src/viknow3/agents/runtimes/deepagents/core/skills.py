"""ViKnow skill discovery and profile resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SkillSpec(BaseModel):
    """Discovered skill metadata used by ViKnow runtime adapters."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    source_dir: Path
    skill_file: Path
    allowed_tools: list[str] = Field(default_factory=list)


def discover_skills(skill_dirs: Sequence[Path]) -> dict[str, SkillSpec]:
    """Discover skills from configured source directories."""

    discovered: dict[str, SkillSpec] = {}
    for skill_root in skill_dirs:
        if not skill_root.exists():
            raise FileNotFoundError(skill_root)
        if not skill_root.is_dir():
            raise ValueError(f"skill_dir must be a directory: {skill_root}")
        for source_dir in sorted(path for path in skill_root.iterdir() if path.is_dir()):
            skill = _load_skill(source_dir)
            if skill.name in discovered:
                first = discovered[skill.name]
                raise ValueError(
                    f"duplicate skill name {skill.name!r}: "
                    f"{first.source_dir} and {skill.source_dir}"
                )
            discovered[skill.name] = skill
    return discovered


def resolve_enabled_skills(
    skill_dirs: Sequence[Path],
    enabled_skills: Sequence[str],
    *,
    enabled_tools: Sequence[str] | None = None,
) -> list[SkillSpec]:
    """Resolve profile skill names into concrete skill specs."""

    if not enabled_skills:
        return []
    discovered = discover_skills(skill_dirs)
    missing = [name for name in enabled_skills if name not in discovered]
    if missing:
        raise RuntimeError(f"unsupported skill(s): {', '.join(sorted(missing))}")
    skills = [discovered[name] for name in enabled_skills]
    if enabled_tools is not None:
        _validate_skill_tool_dependencies(skills, enabled_tools)
    return skills


def _validate_skill_tool_dependencies(
    skills: Sequence[SkillSpec],
    enabled_tools: Sequence[str],
) -> None:
    enabled = set(enabled_tools)
    missing_by_skill = {
        skill.name: sorted(set(skill.allowed_tools) - enabled)
        for skill in skills
        if set(skill.allowed_tools) - enabled
    }
    if missing_by_skill:
        details = "; ".join(
            f"{skill_name} requires {', '.join(missing_tools)}"
            for skill_name, missing_tools in sorted(missing_by_skill.items())
        )
        raise RuntimeError(f"enabled skill tool dependency missing: {details}")


def _load_skill(source_dir: Path) -> SkillSpec:
    skill_file = source_dir / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(skill_file)
    frontmatter = _skill_frontmatter(skill_file)
    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    if not name:
        raise ValueError(f"skill name is required: {skill_file}")
    if not description:
        raise ValueError(f"skill description is required: {skill_file}")
    if name != source_dir.name:
        raise ValueError(f"skill name must match directory name: {name!r} != {source_dir.name!r}")
    return SkillSpec(
        name=name,
        description=description,
        source_dir=source_dir.resolve(),
        skill_file=skill_file.resolve(),
        allowed_tools=_parse_allowed_tools(frontmatter.get("allowed-tools")),
    )


def _skill_frontmatter(skill_file: Path) -> dict:
    content = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if match is None:
        raise ValueError(f"skill frontmatter is required: {skill_file}")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError(f"skill frontmatter must be a mapping: {skill_file}")
    return data


def _parse_allowed_tools(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip(",").strip() for item in raw.split() if item.strip(",").strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValueError("allowed-tools must be a string or list")
