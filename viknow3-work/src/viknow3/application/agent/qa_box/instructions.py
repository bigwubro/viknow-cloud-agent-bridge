"""Runtime-specific instruction overlays on the shared viknow-v2 profile."""

from __future__ import annotations

from viknow3.application.agent.qa_box.profile import ViknowV2Profile

_DEEPAGENTS_SKILL_STEP = """
技能（skills）：知识库问答必须遵循已钉入系统提示的 Pinned skill（如 knowledge-qa-v2），
并按 skill 调用 knowledge_discover / knowledge_search / inspect 等工具。
不要用 ls、glob、grep 在 `/workspace` 里代替知识库检索或列举「库内文件」。
需要补充细节时，再用 read_file 读取 `/workspace/.skills/<技能名>/SKILL.md`；复杂任务可用 write_todos。
""".strip()

def runtime_instruction(profile: ViknowV2Profile, runtime: str) -> str:
    base = profile.system_prompt.strip()
    if runtime == "deepagents":
        return f"{base}\n\n{_DEEPAGENTS_SKILL_STEP}"
    return base
