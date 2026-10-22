"""Runtime-specific instruction overlays on the shared viknow-v2 profile."""

from __future__ import annotations

from viknow3.application.agent.knowledge_assistant.profile import ViknowV2Profile

_DEEPAGENTS_SKILL_STEP = """
技能（skills）：需要详细流程时，用 read_file 读取 `/workspace/.skills/<技能名>/SKILL.md`；复杂任务可用 write_todos 规划步骤。
""".strip()

_ADK_SKILL_STEP = """
技能（skills）：若 list_skills 有可用项，可用 load_skill / load_skill_resource 加载流程。
回答知识库问题时，必须先对本轮授权的 library_id 调用 knowledge_search、discover_libraries 等检索工具获取证据，再组织答案。
""".strip()


def runtime_instruction(profile: ViknowV2Profile, runtime: str) -> str:
    base = profile.system_prompt.strip()
    if runtime == "deepagents":
        return f"{base}\n\n{_DEEPAGENTS_SKILL_STEP}"
    if runtime == "adk":
        return f"{base}\n\n{_ADK_SKILL_STEP}"
    return base
