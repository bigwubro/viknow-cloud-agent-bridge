"""Original single-choice identity and evidence-backed final rendering."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class ChoiceOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    option_id: str
    option_text: str


class ChoiceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str
    option_text: str
    supporting_claim_refs: list[str] = Field(default_factory=list)


class RunProgress(BaseModel):
    """Optional metadata on a normal model tool decision, never tool evidence."""

    model_config = ConfigDict(extra="forbid")
    option_id: str | None = None
    supporting_claim_refs: list[str] = Field(default_factory=list)
    conflicting_claim_refs: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)


class ProvisionalChoice(ChoiceSelection):
    original_question: str
    evidence_digest: str
    # Visible claims at the decision boundary; availability is not assessment.
    evidence_snapshot: dict[str, str] = Field(default_factory=dict)
    conflicting_claim_refs: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    tool_call_id: str


_OPTION = re.compile(r"^\s*([A-Z])[.)：:]\s+(.+?)\s*$")
_FINAL = re.compile(r"(?im)^\s*(?:Final answer|最终选择)\s*[：:]\s*([A-Z])\s*[.。]?\s*$")


def parse_choice_options(question: str) -> list[ChoiceOption]:
    """Read one contiguous labelled block, never infer IDs from option meaning."""
    # Labelled prose alone is not a single-choice output contract.
    if not re.search(
        r"final\s+answer\s*[:：]|最终选择|单选|选择一个|"
        r"answer\s+with\s+(?:one|a\s+single)\s+letter|"
        r"(?:choose|select)\s+(?:one|a\s+single)\s+(?:answer|option)",
        question,
        re.I,
    ):
        return []
    blocks: list[list[ChoiceOption]] = []
    current: list[ChoiceOption] = []
    for line in question.splitlines():
        match = _OPTION.fullmatch(line)
        if match:
            current.append(ChoiceOption(option_id=match[1], option_text=match[2]))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    blocks = [block for block in blocks if len(block) >= 2]
    if len(blocks) != 1:
        return []
    options = blocks[0]
    ids = [item.option_id for item in options]
    if len(set(ids)) != len(ids) or ids != [chr(65 + i) for i in range(len(ids))]:
        return []
    return options


def apply_choice_selection(
    answer: str,
    options: list[ChoiceOption],
    selection: ChoiceSelection | None,
    eligible_claim_refs: set[str],
) -> str:
    if selection is None or not selection.supporting_claim_refs:
        return answer
    original = next((item for item in options if item.option_id == selection.option_id), None)
    if original is None or original.option_text != selection.option_text:
        return answer
    if not set(selection.supporting_claim_refs) <= eligible_claim_refs:
        return answer
    matches = list(_FINAL.finditer(answer))
    if len(matches) != 1:
        return answer
    match = matches[0]
    return answer[: match.start(1)] + selection.option_id + answer[match.end(1) :]


def hold_final_choice(text: str) -> tuple[str, str]:
    """Keep only the terminating choice line out of provisional streamed prose."""
    match = re.search(r"(?im)^\s*(?:Final answer|最终选择)\s*[：:]", text)
    return (text[: match.start()], text[match.start() :]) if match else (text, "")


def render_supported_choice(options, selection, facts, unresolved) -> str | None:
    """Render only original option text and traceable facts after a correction."""
    if selection is None or not selection.supporting_claim_refs:
        return None
    option = next((o for o in options if o.option_id == selection.option_id), None)
    if option is None or option.option_text != selection.option_text:
        return None
    eligible = {}
    for fact in facts:
        if fact.get("status") != "verified":
            continue
        for key in (
            fact.get("claim_ref"),
            fact.get("claim_id"),
            f"[{fact['citation_index']}]" if fact.get("citation_index") else None,
        ):
            if key:
                eligible.setdefault(key, []).append(fact)
    supports = []
    for ref in selection.supporting_claim_refs:
        candidates = eligible.get(ref, [])
        if len(candidates) != 1:
            return None
        if candidates[0] not in supports:
            supports.append(candidates[0])
    lines = []
    for fact in supports:
        marker = fact.get("citation") or (
            f"[{fact['citation_index']}]" if fact.get("citation_index") else ""
        )
        lines.append(str(fact.get("claim") or "").strip('"') + marker)
    lines.append(f"选项 {option.option_id}：{option.option_text}")
    if unresolved:
        lines.append("部分原题条件或比较证据仍未核验。这是低置信选择。")
    lines.append(f"Final answer: {option.option_id}")
    return "\n".join(lines)


def terminal_choice_answer(
    question: str,
    draft: str,
    *,
    selection: ChoiceSelection | None = None,
    termination_reason: str = "budget_exhausted",
    prefer_selection: bool = False,
) -> tuple[str, str]:
    options = parse_choice_options(question)
    if not options:
        return "", ""
    matches = list(_FINAL.finditer(draft))
    option = next((o for o in options if matches and o.option_id == matches[-1][1].upper()), None)
    reason = "retained_draft_choice" if option else "forced_choice_without_evidence"
    if selection is not None and (option is None or prefer_selection):
        selected_option = next(
            (
                o
                for o in options
                if o.option_id == selection.option_id and o.option_text == selection.option_text
            ),
            None,
        )
        if selected_option is not None:
            option = selected_option
            reason = "retained_provisional_choice"
    option = option or options[0]
    introduction = {
        "budget_exhausted": "预算已耗尽",
        "model_exception": "模型调用失败，回答未能完成",
        "runtime_exception": "运行发生异常，回答未能完成",
        "missing_final_choice": "回答结束时未给出有效选项",
    }[termination_reason]
    return (
        f"{introduction}，未完成的条件仍未核验。以下为低置信选择。\n"
        f"选项 {option.option_id}：{option.option_text}\nFinal answer: {option.option_id}",
        reason,
    )


def budget_choice_answer(
    question: str, draft: str, *, selection: ChoiceSelection | None = None
) -> tuple[str, str]:
    return terminal_choice_answer(question, draft, selection=selection)


def explicit_choice_checkpoint(text: str) -> tuple[bool, str | None]:
    """Read only standalone decision markers, never prose, quotations or code."""
    selected: tuple[bool, str | None] = (False, None)
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if fence:
            continue
        match = re.fullmatch(
            r"(?:Current choice|Tentative choice|Final answer|最终选择)\s*[:：]\s*"
            r"([A-Z]|unknown|none|null)\s*[.。]?",
            stripped,
            re.I,
        )
        if match:
            value = match[1].upper()
            selected = (True, value if len(value) == 1 else None)
    return selected


def has_final_choice(question: str, draft: str) -> bool:
    ids = {option.option_id for option in parse_choice_options(question)}
    return any(match[1].upper() in ids for match in _FINAL.finditer(draft))


def unverified_choice_answer(question: str, draft: str) -> str:
    answer, _ = budget_choice_answer(question, draft)
    return answer.replace("预算已耗尽，", "复核未能确认全部回答依据，")
