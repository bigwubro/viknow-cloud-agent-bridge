"""Load the shared ADK profile and skill text."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_AGENTS = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_profile() -> dict:
    path = _AGENTS / "profiles" / "expert_buddy_adk.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError("ADK profile is not a mapping")
    return data


def skill_body(name: str) -> str:
    from agents.qa_text_plan import present_skill

    path = _AGENTS / "skills" / name / "SKILL.md"
    if not path.is_file():
        return ""
    return present_skill(_strip_frontmatter(path.read_text(encoding="utf-8")).strip())


def build_instruction(library_ids: tuple[str, ...]) -> str:
    data = load_profile()
    parts = [str(data.get("system_prompt") or "").strip()]
    for name in data.get("enabled_skills") or []:
        body = skill_body(str(name))
        if body:
            parts.append(body)
    granted = ", ".join(library_ids) if library_ids else "(none)"
    parts.append(f"本轮授予的知识库：{granted}")
    tools = ", ".join(str(item) for item in data.get("enabled_tools") or [])
    parts.append(f"本轮可以调用的工具：{tools}")
    return "\n\n".join(part for part in parts if part)


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :]
