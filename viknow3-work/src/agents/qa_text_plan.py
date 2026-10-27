"""Text answer plan from the knowledge-qa-v2 skill: content plus findings."""

from __future__ import annotations

import re
from typing import Any

_ALIAS = re.compile(r"\[[A-Za-z0-9_]+\]")
_CONTENT_LIMIT = 6000
_FINDING_LIMIT = 40
_TEXT_LIMIT = 2000
_TEMPLATES = {"generic", "comparison"}
_TEXT_KEYS = ("content", "findings", "template_id")

FORMAT_PLACEHOLDER = "{{qa_final_answer_format_examples}}"
FORMAT_REPLACEMENT = (
    "Answer format examples live in references/answer-templates.md. "
    "Read that file when a template is selected. Those examples are not evidence, "
    "and their aliases are not assigned to this question."
)


def empty_plan() -> dict[str, Any]:
    return {
        "content": "",
        "findings": [],
        "template_id": "generic",
        "reviewed": False,
        "started": False,
    }


def is_text_plan_call(args: dict[str, Any]) -> bool:
    return any(key in args for key in _TEXT_KEYS)


def present_skill(text: str) -> str:
    return text.replace(FORMAT_PLACEHOLDER, FORMAT_REPLACEMENT)


def apply_text_plan(plan: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    action = str(args.get("action") or "")
    if action not in {"view", "start", "update", "review"}:
        return plan, "action must be view, start, update, or review."
    if action == "view":
        if is_text_plan_call(args):
            return plan, "view omits content, findings, and template_id."
        return dict(plan), None
    content = args.get("content")
    findings = args.get("findings")
    template_id = args.get("template_id")
    if template_id is not None and template_id not in _TEMPLATES:
        return plan, "template_id must be generic or comparison."
    if action in {"start", "update"} and (not isinstance(content, str) or not content.strip()):
        return plan, "start and update require content."
    if content is not None and not isinstance(content, str):
        return plan, "content must be a string."
    if isinstance(content, str) and len(content) > _CONTENT_LIMIT:
        return plan, "content is longer than 6000 characters."
    if isinstance(content, str) and _ALIAS.search(content):
        return plan, "content must not contain citation aliases."
    parsed, error = _findings(findings)
    if error:
        return plan, error
    next_plan = {
        "content": str(plan.get("content") or ""),
        "findings": list(plan.get("findings") or []),
        "template_id": plan.get("template_id") or "generic",
        "reviewed": bool(plan.get("reviewed")),
        "started": bool(plan.get("started")),
    }
    if action == "start":
        if next_plan["started"]:
            return plan, "The plan already exists. Use action=update."
        next_plan["content"] = content.strip()
        next_plan["findings"] = parsed if parsed is not None else []
        next_plan["started"] = True
        next_plan["reviewed"] = False
    elif action == "update":
        if not next_plan["started"]:
            return plan, "Call action=start before update."
        next_plan["content"] = content.strip()
        next_plan["findings"] = parsed if parsed is not None else []
        next_plan["reviewed"] = False
    else:
        if not next_plan["started"]:
            return plan, "Call action=start before review."
        if isinstance(content, str) and content.strip():
            next_plan["content"] = content.strip()
        if parsed is not None:
            next_plan["findings"] = parsed
        next_plan["reviewed"] = True
    if template_id:
        if template_id != next_plan["template_id"]:
            next_plan["reviewed"] = action == "review"
        next_plan["template_id"] = template_id
    return next_plan, None


def render_answer(question: str, plan: dict[str, Any]) -> str:
    chinese = any("\u4e00" <= char <= "\u9fff" for char in question)
    prefix = "答案：" if chinese else "Answer: "
    findings = plan.get("findings") or []
    if findings:
        lines = [
            f"{item['text']}{''.join(item.get('claim_refs') or [])}"
            for item in findings
        ]
        return prefix + "\n".join(lines)
    note = str(plan.get("content") or "").strip()
    if not note:
        note = "无法从当前资料确定。" if chinese else "The current sources do not establish this."
    return prefix + note


def final_check(question: str, plan: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not plan.get("reviewed"):
        return (
            {
                "submitted": False,
                "instruction": (
                    "Call answer_plan with action=review and content describing completed work, "
                    "remaining gaps, and why investigation can end. Then call final_check() alone."
                ),
            },
            "",
        )
    return {"submitted": True}, render_answer(question, plan)


def _findings(raw: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "findings must be a JSON array."
    if len(raw) > _FINDING_LIMIT:
        return None, "findings is longer than 40 items."
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"findings[{index}] must be an object."
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, f"findings[{index}].text is required."
        if len(text) > _TEXT_LIMIT:
            return None, f"findings[{index}].text is longer than 2000 characters."
        if _ALIAS.search(text):
            return None, f"findings[{index}].text must not contain citation aliases."
        refs = item.get("claim_refs", [])
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or _ALIAS.fullmatch(ref) is None for ref in refs
        ):
            return None, f"findings[{index}].claim_refs must be aliases such as [C1]."
        if len(refs) != len(set(refs)):
            return None, f"findings[{index}].claim_refs contains duplicates."
        items.append({"text": text.strip(), "claim_refs": list(refs)})
    return items, None
