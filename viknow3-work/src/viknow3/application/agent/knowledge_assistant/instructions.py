"""Runtime-specific instruction overlays on the shared viknow-v2 profile."""

from __future__ import annotations

from viknow3.application.agent.knowledge_assistant.profile import ViknowV2Profile

_DEEPAGENTS_SKILL_STEP = """
技能（skills）：需要详细流程时，用 read_file 读取对应 SKILL.md；复杂任务可用 write_todos 规划步骤。
""".strip()

_ADK_SKILL_STEP = """
技能（skills）：先用 list_skills 查看可用技能，再用 load_skill 加载正文；需要资源文件时用 load_skill_resource。
""".strip()


def runtime_instruction(profile: ViknowV2Profile, runtime: str) -> str:
    base = profile.system_prompt.strip()
    if runtime == "deepagents":
        return f"{base}\n\n{_DEEPAGENTS_SKILL_STEP}"
    if runtime == "adk":
        return f"{base}\n\n{_ADK_SKILL_STEP}"
    return base
