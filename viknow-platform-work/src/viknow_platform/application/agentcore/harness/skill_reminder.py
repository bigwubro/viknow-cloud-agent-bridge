"""Skill reminder text (viknow2 _render_skill_reminder semantics, shortened)."""

from __future__ import annotations


def render_skill_reminder(skill_name: str, skill_path: str) -> str:
    return (
        f"\n\n## Skill reminder\n"
        f"- Primary skill: `{skill_name}`\n"
        f"- Skill file: `{skill_path}`\n"
        f"- First workflow tool call must follow reading that SKILL.md via `read_file`.\n"
        f"- Never glob, ls, or grep the filesystem to find SKILL.md.\n"
        f"- For authorized libraries, use knowledge_discover / knowledge_search; do not guess from workspace files.\n"
    )
