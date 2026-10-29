"""Load viknow-v2 parity profile (mirrors viknow2 config/profiles/viknow-v2.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PROFILE_PATH = Path(__file__).resolve().parent / "profile" / "viknow-v2.yaml"


@dataclass(frozen=True)
class AnswerReviewConfig:
    enabled: bool = True
    gate_enabled: bool = True
    pass_threshold: int = 75
    max_retries: int = 1
    allow_low_confidence_output: bool = True


@dataclass(frozen=True)
class RuntimeBudgetConfig:
    enabled: bool = True
    max_recursion_steps: int = 60
    time_limit_seconds: int = 600
    warn_ratio: float = 0.6


@dataclass(frozen=True)
class ViknowV2Profile:
    id: str
    label: str
    description: str
    enabled_tools: tuple[str, ...]
    enabled_skills: tuple[str, ...]
    system_prompt: str
    user_prompt_template: str
    answer_review: AnswerReviewConfig
    runtime_budget: RuntimeBudgetConfig


def load_viknow_v2_profile(path: Path | None = None) -> ViknowV2Profile:
    source = path or _PROFILE_PATH
    raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))
    review = raw.get("answer_review") or {}
    budget = raw.get("runtime_budget") or {}
    return ViknowV2Profile(
        id=str(raw["id"]),
        label=str(raw["label"]),
        description=str(raw["description"]),
        enabled_tools=tuple(str(item) for item in raw.get("enabled_tools") or ()),
        enabled_skills=tuple(str(item) for item in raw.get("enabled_skills") or ()),
        system_prompt=str(raw.get("system_prompt") or "").strip(),
        user_prompt_template=str(raw.get("user_prompt_template") or "{input}"),
        answer_review=AnswerReviewConfig(
            enabled=bool(review.get("enabled", True)),
            gate_enabled=bool(review.get("gate_enabled", True)),
            pass_threshold=int(review.get("pass_threshold", 75)),
            max_retries=int(review.get("max_retries", 1)),
            allow_low_confidence_output=bool(review.get("allow_low_confidence_output", True)),
        ),
        runtime_budget=RuntimeBudgetConfig(
            enabled=bool(budget.get("enabled", True)),
            max_recursion_steps=int(budget.get("max_recursion_steps", 60)),
            time_limit_seconds=int(budget.get("time_limit_seconds", 600)),
            warn_ratio=float(budget.get("warn_ratio", 0.6)),
        ),
    )
