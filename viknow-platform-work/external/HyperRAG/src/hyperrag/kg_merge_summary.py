from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _dedupe_keep_order(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(value)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(normalized)
    return out


def _merge_unique_text(left: str, right: str) -> str:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    if left_norm == right_norm:
        return left_norm
    return f"{left_norm}\n{right_norm}"


def rough_token_count(value: str) -> int:
    text = _normalize_text(value)
    if not text:
        return 0
    return max(1, len(text) // 2)


@dataclass
class EntityMergeSummaryHelper:
    llm_func: Optional[Callable[..., Awaitable[str]]]
    enabled: bool = True
    trigger_fragments: int = 24
    trigger_tokens: int = 8000
    context_size: int = 16000
    max_output_tokens: int = 1200

    def should_summarize(self, descriptions: List[str]) -> bool:
        merged = "\n".join(_dedupe_keep_order(descriptions))
        return len(descriptions) >= max(2, int(self.trigger_fragments)) or rough_token_count(merged) >= int(
            self.trigger_tokens
        )

    async def merge_descriptions(
        self,
        *,
        entity_name: str,
        entity_type: str,
        aliases: List[str],
        descriptions: List[str],
    ) -> str:
        merged = ""
        unique_descriptions = _dedupe_keep_order(descriptions)
        for description in unique_descriptions:
            merged = _merge_unique_text(merged, description)
        if not merged:
            return ""
        if not self.enabled or self.llm_func is None or not self.should_summarize(unique_descriptions):
            return merged
        prompt_payload = {
            "entity_name": entity_name,
            "entity_type": entity_type,
            "aliases": aliases,
            "rules": [
                "Summarize repeated merged entity descriptions into one concise technical description.",
                "Preserve stable identifying details, model numbers, roles, and scope.",
                "Do not invent new facts.",
                "Return plain text only.",
                "Keep it shorter than the input and readable for retrieval.",
            ],
            "descriptions": unique_descriptions,
        }
        prompt_text = json.dumps(prompt_payload, ensure_ascii=False)
        if len(prompt_text) > int(self.context_size):
            prompt_text = prompt_text[: int(self.context_size)]
        result = await self.llm_func(
            prompt_text,
            system_prompt="You compress merged knowledge-graph entity descriptions into one precise retrieval-friendly summary.",
            max_tokens=int(self.max_output_tokens),
        )
        normalized = _normalize_text(result)
        return normalized or merged