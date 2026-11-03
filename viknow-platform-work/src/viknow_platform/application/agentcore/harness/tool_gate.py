"""Admission rules before KB workflow tools run (viknow2 tool_gate subset)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from viknow_platform.application.agent.knowledge_assistant.library_scope import resolve_library_id
from viknow_platform.application.agentcore.harness.facts import HarnessRunFacts

WORKFLOW_TOOLS = frozenset(
    {
        "knowledge_discover",
        "uploaded_image_search",
        "query_refine",
        "answer_plan",
        "knowledge_search",
        "video_inspect_2_0",
        "visual_ground",
        "document_inspect",
        "list_video_segments",
        "list_doc_segments",
    }
)

WORKSPACE_GUESS_TOOLS = frozenset({"ls", "glob", "grep", "execute"})

LIBRARY_ARG_TOOLS = frozenset(
    {
        "answer_plan",
        "knowledge_search",
        "video_inspect_2_0",
        "document_inspect",
        "list_video_segments",
        "list_doc_segments",
        "uploaded_image_search",
    }
)


@dataclass(frozen=True)
class GateDecision:
    rule: str
    message: str
    recovery: str


def skill_path_matches(path: str, skill_name: str) -> bool:
    normalized = (path or "").replace("\\", "/")
    return f"/{skill_name}/SKILL.md" in normalized or normalized.endswith(f"{skill_name}/SKILL.md")


def evaluate_tool_gate(tool_name: str, args: dict[str, Any], facts: HarnessRunFacts, *, primary_skill: str) -> GateDecision | None:
    if tool_name == "read_file":
        path = str(args.get("path") or args.get("file_path") or "")
        if skill_path_matches(path, primary_skill):
            return None
        if facts.library_ids and not facts.skill_loaded:
            return GateDecision(
                "required_skill_not_loaded",
                f"先 read_file 加载 `/workspace/.skills/{primary_skill}/SKILL.md`，再调用其他工具。",
                f"read_file path=/workspace/.skills/{primary_skill}/SKILL.md",
            )
        return None

    if tool_name in WORKSPACE_GUESS_TOOLS and facts.library_ids:
        return GateDecision(
            "kb_first_no_workspace_guess",
            "本轮已授权知识库，禁止用 ls/glob/grep/execute 在 workspace 猜答案；按 skill 调用 knowledge_* / inspect。",
            "knowledge_discover → query_refine → knowledge_search",
        )

    if tool_name in WORKFLOW_TOOLS and not facts.skill_loaded:
        return GateDecision(
            "required_skill_not_loaded",
            f"workflow 工具 `{tool_name}` 需要先加载 skill `{primary_skill}`。",
            f"read_file /workspace/.skills/{primary_skill}/SKILL.md",
        )

    if tool_name == "knowledge_discover" and not facts.library_ids:
        return GateDecision(
            "no_library_granted",
            "当前会话未授权 library_id；请在 Playground 选择知识库。",
            "选择知识库后新建或继续会话",
        )

    if tool_name in LIBRARY_ARG_TOOLS:
        raw = args.get("library_id")
        library_id = resolve_library_id(str(raw) if raw is not None else "")
        allowed = facts.run_library_ids()
        if allowed and library_id and library_id not in allowed:
            return GateDecision(
                "library_scope",
                f"library_id `{library_id}` 不在本轮授权范围 {sorted(allowed)}。",
                "使用本轮授权的 library_id，或先 knowledge_discover 选库。",
            )

    return None
