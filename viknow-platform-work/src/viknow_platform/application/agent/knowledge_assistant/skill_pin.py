"""Pin skill SKILL.md excerpts into the system prompt (deepagents does not auto-load like ADK load_skill)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
_DEFAULT_MAX_CHARS = 6000


def pinned_skills_prompt(enabled_skills: Sequence[str], *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Embed enabled skills so the model follows workflow without relying on read_file alone."""

    blocks: list[str] = []
    for name in enabled_skills:
        skill_id = str(name).strip()
        if not skill_id:
            continue
        path = _SKILLS_ROOT / skill_id / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > max_chars:
            text = text[: max_chars - 20].rstrip() + "\n…(truncated)"
        blocks.append(f"## Pinned skill `{skill_id}`\n\n{text}")
    if not blocks:
        return ""
    return (
        "\n\n---\nThe following skill documents are authoritative for this run. "
        "Follow them before calling workflow tools; do not improvise from memory.\n\n"
        + "\n\n".join(blocks)
        + "\n---\n"
    )
